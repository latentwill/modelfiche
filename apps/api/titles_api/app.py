from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text

from . import __version__, models
from .database import Base, SessionLocal, engine
from .routers import collaboration, compat, core, datasets, integrations, operator, operator_config, runs
from .settings import get_settings
from .integrations.config import S3Settings
from .integrations.fal.adapters import list_adapters


def initialize_database() -> None:
    settings = get_settings()
    settings.ensure_runtime_dirs()
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        workspace = db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
        if workspace is None:
            workspace = models.Workspace(name=settings.default_workspace_name)
            db.add(workspace)
            db.flush()
        profile = db.scalar(select(models.UserProfile).where(models.UserProfile.workspace_id == workspace.id).order_by(models.UserProfile.created_at))
        if profile is None:
            profile = models.UserProfile(workspace_id=workspace.id, display_name=settings.default_profile_name, initials="OP", avatar_color="#365B6D")
            db.add(profile)
            db.flush()
        preference = db.scalar(select(models.LocalPreference).where(models.LocalPreference.key == "active_profile"))
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

    @app.get("/health")
    @app.get("/api/health")
    def health():
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    @app.get("/api/system/version")
    def version():
        return {"name": "titles-dam", "version": __version__, "api_version": "1"}

    @app.get("/api/system/capabilities")
    def capabilities():
        return {
            "profiles": True,
            "auth": False,
            "s3_read_only": True,
            "dataset_drafts": True,
            "training_runs": True,
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
        return {
            "ok": database_ok,
            "database": {"ok": database_ok, "url": "sqlite" if settings.database_url.startswith("sqlite") else "configured"},
            "asset_store": {"ok": settings.asset_root.exists(), "path": str(settings.asset_root)},
            "cache": {"ok": settings.cache_root.exists(), "path": str(settings.cache_root)},
            "s3": {
                "configured": bool(
                    (os.getenv("S3_BUCKET") and os.getenv("S3_ALLOWED_PREFIXES"))
                    or (os.getenv("MEGA_BUCKET") and os.getenv("MEGA_ALLOWED_PREFIXES"))
                )
            },
            "fal": {"configured": bool(os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY"))},
        }

    for api_router in (core.router, datasets.router, runs.router, collaboration.router, operator.router, operator_config.router):
        app.include_router(api_router, prefix="/api")
    app.include_router(compat.router, prefix="/api")
    app.include_router(integrations.router)
    return app


app = create_app()
