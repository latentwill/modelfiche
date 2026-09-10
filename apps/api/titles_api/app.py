from contextlib import asynccontextmanager
import os
from datetime import datetime, timezone
from pathlib import Path
import shutil

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from . import __version__, models
from .database import SessionLocal, bootstrap_empty_local_database, engine as engine, upgrade_installed_database
from .routers import (
    asset_delivery,
    collaboration,
    compat,
    core,
    datasets,
    fal_admissions,
    generation_queue,
    embedding_imports,
    grid_eval,
    integrations,
    local_imports,
    merge_operations,
    operator,
    operator_config,
    runs,
    training_launches,
    wandb_credentials,
    storage_policy,
)
from .support_bundle import create_support_bundle
from .settings import get_settings
from .integrations.config import S3Settings
from .integrations.fal.adapters import list_adapters
from .integrations.fal.credentials import resolve_fal_credential
from .security_middleware import LocalLaunchContract, TrustedLocalBoundary
from .services import WorkspaceContextMiddleware


def initialize_database() -> bool:
    settings = get_settings()
    settings.ensure_runtime_dirs()
    upgrade_installed_database(settings.database_url)
    bootstrap_empty_local_database(settings.database_url)
    with SessionLocal.begin() as db:
        if not _storage_contract_ready(db):
            return False
        workspace = db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
        if workspace is None:
            workspace = models.Workspace(name=settings.default_workspace_name)
            db.add(workspace)
            db.flush()
        profile = db.scalar(
            select(models.UserProfile)
            .where(models.UserProfile.workspace_id == workspace.id)
            .order_by(models.UserProfile.created_at)
        )
        if profile is None:
            profile = models.UserProfile(
                workspace_id=workspace.id,
                display_name=settings.default_profile_name,
                initials="OP",
                avatar_color="#365B6D",
            )
            db.add(profile)
            db.flush()
        preference = db.scalar(
            select(models.LocalPreference).where(models.LocalPreference.key == "active_profile")
        )
        if preference is None:
            db.add(models.LocalPreference(key="active_profile", value={"profile_id": profile.id}))
        for env_prefix, name in (("S3", "S3"), ("MEGA", "Mega S4")):
            try:
                connection = S3Settings.from_env(env_prefix, legacy_fallback=False)
            except ValueError:
                continue
            source = db.scalar(
                select(models.ImportSource).where(
                    models.ImportSource.workspace_id == workspace.id,
                    models.ImportSource.name == name,
                )
            )
            values = {
                "endpoint_url": connection.endpoint_url,
                "region": connection.region,
                "addressing_style": connection.addressing_style,
                "credential_env_prefix": connection.credential_env_prefix,
                "bucket": connection.bucket,
                "allowed_prefixes": list(connection.allowed_prefixes),
                "is_active": True,
            }
            if source is None:
                db.add(models.ImportSource(workspace_id=workspace.id, name=name, provider="s3", **values))
            else:
                for key, value in values.items():
                    setattr(source, key, value)
    return True


def _storage_contract_ready(db) -> bool:
    marker = db.execute(
        text("SELECT marker FROM storage_cutover_marker WHERE id=1")
    ).scalar_one_or_none()
    if marker != 1:
        return False
    state = db.execute(
        text("SELECT state FROM storage_feature_gates WHERE name='storage_contract'")
    ).scalar_one_or_none()
    return state == "ready"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    yield


def create_app(launch_contract: LocalLaunchContract) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(TrustedLocalBoundary, contract=launch_contract)
    app.add_middleware(WorkspaceContextMiddleware)

    @app.get("/health")
    @app.get("/api/health")
    def health():
        checked_at = datetime.now(timezone.utc)
        components: dict[str, dict[str, object]] = {}
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
                latest_job = db.scalar(select(func.max(models.Job.updated_at)))
                latest_source = db.scalar(select(func.max(models.ImportSource.updated_at)))
                queued_jobs = int(db.scalar(select(func.count(models.Job.id)).where(models.Job.state == models.JobState.queued)) or 0)
                oldest_queued = db.scalar(select(func.min(models.Job.created_at)).where(models.Job.state == models.JobState.queued))
                heartbeat = db.scalar(select(models.WorkerHeartbeat).order_by(models.WorkerHeartbeat.heartbeat_at.desc()).limit(1))
            components["api_db"] = {"status": "ok", "evidence": "SELECT 1", "checked_at": checked_at.isoformat()}
            components["queue"] = {
                "status": "ok",
                "evidence": "job table readable",
                "freshness": latest_job.isoformat() if latest_job else None,
                "depth": queued_jobs,
                "oldest_wait_seconds": max(0, int((checked_at - oldest_queued).total_seconds())) if oldest_queued else None,
            }
            heartbeat_age = (checked_at - heartbeat.heartbeat_at).total_seconds() if heartbeat else None
            heartbeat_ok = bool(heartbeat and heartbeat.state == "running" and heartbeat_age is not None and heartbeat_age <= 10)
            components["worker_heartbeat"] = {
                "status": "ok" if heartbeat_ok else "unavailable",
                "evidence": "dedicated worker heartbeat",
                "freshness": heartbeat.heartbeat_at.isoformat() if heartbeat else None,
                "age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
                "worker_id": heartbeat.worker_id if heartbeat else None,
                "worker_version": heartbeat.version if heartbeat else None,
                "pid": heartbeat.pid if heartbeat else None,
            }
            components["storage"] = {"status": "ok" if all(path.exists() and path.is_dir() for path in (settings.asset_root, settings.cache_root, settings.export_root)) else "degraded", "evidence": "configured local roots", "checked_at": checked_at.isoformat()}
            components["source_freshness"] = {"status": "unknown", "evidence": "latest configured import source update", "freshness": latest_source.isoformat() if latest_source else None}
            components["provider_freshness"] = {"status": "configuration_only", "evidence": "credentials/configuration inspected; no provider network check", "checked_at": checked_at.isoformat()}
        except Exception as exc:
            components["api_db"] = {"status": "error", "evidence": str(exc), "checked_at": checked_at.isoformat()}
        status_value = "ok" if components.get("api_db", {}).get("status") == "ok" and components.get("worker_heartbeat", {}).get("status") == "ok" else "degraded"
        return {"status": status_value, "version": __version__, "checked_at": checked_at.isoformat(), "components": components}

    @app.get("/api/system/version")
    def version():
        return {"name": "titles-dam", "version": __version__, "api_version": "1"}

    @app.get("/api/system/capabilities")
    def capabilities():
        return {
            "profiles": True,
            "auth": False,
            "s3_read_only": False,
            "dataset_drafts": True,
            "training_runs": True,
            "workspaces": True,
            "agent_contract": {
                "openapi_path": "/openapi.json",
                "workspace_header": "X-Workspace-ID",
                "workspace_reference": "id_or_slug",
                "workspace_required_for_mutations": True,
                "unscoped_reads": "oldest_workspace",
                "profile_header": "X-Profile-ID",
                "idempotency_header": "Idempotency-Key",
                "training_readiness_path": "/api/training-setup",
                "training_preflight_path": "/api/training-launches/{launch_id}/preflight",
                "run_observation_paths": [
                    "/api/runs/{run_id}/live",
                    "/api/runs/{run_id}/metrics",
                    "/api/runs/{run_id}/samples",
                    "/api/runs/{run_id}/checkpoints",
                ],
                "asset_inspection_paths": [
                    "/api/assets/{asset_id}/locations",
                    "/api/assets/{asset_id}/metadata",
                ],
                "supported_workflow_is_not_readiness": True,
            },
            "fal_endpoints": [adapter.endpoint_id for adapter in list_adapters()],
            "reviews": True,
            "client_handoff": False,
        }

    @app.get("/api/system/enums")
    def enums():
        return {
            "project_state": [item.value for item in models.ProjectState],
            "asset_kind": [item.value for item in models.AssetKind],
            "job_state": [item.value for item in models.JobState],
            "model_version_state": ["candidate", "approved", "archived"],
            "review_decision": ["candidate", "approved", "hold", "reject"],
        }

    @app.get("/api/system/doctor")
    def doctor():
        database_ok = False
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            database_ok = True
        except Exception:
            pass
        app_support = Path(os.environ["TITLES_APP_SUPPORT"]).expanduser() if os.getenv("TITLES_APP_SUPPORT") else None
        backup_root = Path(os.getenv("TITLES_BACKUP_ROOT", str(app_support.parent / f"{app_support.name}-backups"))) if app_support else None
        latest_backup = max(backup_root.glob("modelfiche-*.tar.gz"), key=lambda path: path.stat().st_mtime, default=None) if backup_root and backup_root.exists() else None
        embedded_cli = Path(os.environ["TITLES_CLI_PATH"]).expanduser() if os.getenv("TITLES_CLI_PATH") else None
        user_cli = Path.home() / ".local" / "bin" / ("mfiche.cmd" if os.name == "nt" else "mfiche")
        path_cli = shutil.which("mfiche")
        cli_path = str(user_cli) if user_cli.is_file() and os.access(user_cli, os.X_OK) else path_cli
        if cli_path is None and embedded_cli and embedded_cli.is_file():
            cli_path = str(embedded_cli)
        return {
            "ok": database_ok,
            "version": __version__,
            "database": {"ok": database_ok, "url": "sqlite" if settings.database_url.startswith("sqlite") else "configured"},
            "asset_store": {"ok": settings.asset_root.exists(), "path": str(settings.asset_root)},
            "cache": {"ok": settings.cache_root.exists(), "path": str(settings.cache_root)},
            "s3": {
                "configured": bool(
                    os.getenv("S3_BUCKET")
                    or os.getenv("MEGA_BUCKET")
                )
            },
            "fal": {"configured": resolve_fal_credential() is not None},
            "backup": {
                "configured": latest_backup is not None,
                "latest": str(latest_backup) if latest_backup else None,
                "next_action": None if latest_backup else "Run `mfiche backup create`.",
            },
            "agent_cli": {
                "configured": cli_path is not None,
                "path": cli_path,
                "install_path": str(user_cli),
                "install_available": bool(embedded_cli and embedded_cli.is_file() and os.access(embedded_cli, os.X_OK)),
                "next_action": None if cli_path else "Install the `mfiche` command.",
            },
        }

    @app.post("/api/system/cli-install")
    def install_cli():
        source_value = os.getenv("TITLES_CLI_PATH")
        source = Path(source_value).expanduser().resolve() if source_value else None
        if source is None or not source.is_file() or not os.access(source, os.X_OK):
            raise HTTPException(status_code=409, detail="the packaged mfiche executable is unavailable")
        target = Path.home() / ".local" / "bin" / ("mfiche.cmd" if os.name == "nt" else "mfiche")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        managed_windows_shim = os.name == "nt" and target.is_file() and target.read_bytes().startswith(b"@rem Modelfiche CLI launcher\r\n")
        if target.exists() and not target.is_symlink() and not managed_windows_shim:
            raise HTTPException(status_code=409, detail=f"{target} already exists and was not created by Modelfiche")
        temporary = target.with_name(f".{target.name}.{os.getpid()}.install")
        temporary.unlink(missing_ok=True)
        try:
            if os.name == "nt":
                escaped = str(source).replace("%", "%%")
                temporary.write_bytes((f'@rem Modelfiche CLI launcher\r\n@echo off\r\n"{escaped}" %*\r\n').encode("utf-8"))
            else:
                os.symlink(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "ok": True,
            "path": str(target),
            "target": str(source),
            "path_note": f"Add {target.parent} to PATH if your shell does not already include it.",
        }


    @app.get("/api/system/support-bundle")
    def support_bundle():
        with SessionLocal() as db:
            payload = create_support_bundle(
                db,
                database_url=settings.database_url,
                asset_root=settings.asset_root,
                cache_root=settings.cache_root,
                export_root=settings.export_root,
            )
        filename = f"modelfiche-support-{datetime.now(timezone.utc).date().isoformat()}.zip"
        return Response(
            content=payload,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


    for api_router in (
        core.router,
        datasets.router,
        embedding_imports.router,
        runs.router,
        wandb_credentials.router,
        training_launches.router,
        grid_eval.router,
        collaboration.router,
        merge_operations.router,
        operator.router,
        operator_config.router,
        storage_policy.router,
        asset_delivery.router,
        fal_admissions.router,
        generation_queue.router,
    ):
        app.include_router(api_router, prefix="/api")
    app.include_router(compat.router, prefix="/api")
    app.include_router(integrations.router)
    app.include_router(local_imports.router, prefix="/api")
    web_dist = os.getenv("TITLES_WEB_DIST")
    if web_dist:
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app
