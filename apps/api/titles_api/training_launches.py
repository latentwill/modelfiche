from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .dataset_versions import content_digest
from .integrations.fal.adapters import list_adapters
from .settings import get_settings


MANIFEST_VERSION = "modelfiche.training-launch.v1"
WANDB_SDK_VERSION = "0.28.0"
WANDB_PROTOCOL_REVISION = "wandb-0.28.0-ai-toolkit-v1"
TOKEN_FORMAT = "MF1_BASE32_ED25519"
BACKUP_SCHEMA_VERSION = "modelfiche.run-backup.v1"
CHECKPOINT_MANIFEST_VERSION = "modelfiche.checkpoint-manifest.v1"


class LaunchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    workspace_id: str
    profile_id: str | None
    dataset_version_id: str
    credential_id: str
    source_id: str
    client_request_id: str
    name: str
    trainer: str
    base_model: str
    output_directory: str
    checkpoint_policy: dict[str, Any]
    backup_policy: dict[str, Any]
    supported_endpoint_ids: list[str]
    expected_duration_seconds: int
    base_url: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _project_slug(title: str, project_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    return slug[:240] or project_id


def _allocate_wandb_run_id(session: Session) -> str:
    for _ in range(8):
        candidate = secrets.token_hex(4)
        exists = session.scalar(select(models.TrainingRun.id).where(models.TrainingRun.wandb_run_id == candidate))
        if exists is None:
            return candidate
    raise LaunchError("unable to allocate a unique W&B run ID")


def _source_allows_prefix(source: models.ImportSource, prefix: str) -> bool:
    allowed = [str(value).strip("/") + "/" for value in (source.allowed_prefixes or []) if str(value).strip("/")]
    return not allowed or any(prefix.startswith(value) for value in allowed)


def _backup_configuration(
    request: LaunchRequest,
    source: models.ImportSource,
    run_prefix: str,
    callback_url: str,
) -> dict[str, object]:
    interval = request.backup_policy.get("interval_seconds", 300)
    if not isinstance(interval, int) or isinstance(interval, bool) or not 30 <= interval <= 3600:
        raise LaunchError("backup interval_seconds must be between 30 and 3600")
    return {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "source_directory": request.output_directory,
        "schedule": {
            "interval_seconds": interval,
            "run_on_finish": True,
            "resume_from_remote": True,
        },
        "destination": {
            "source_id": source.id,
            "bucket": source.bucket,
            "prefix": run_prefix,
            "addressing_style": source.addressing_style,
            "region": source.region,
            "endpoint_url": source.endpoint_url,
        },
        "credential_environment": {
            "access_key": f"{source.credential_env_prefix}_ACCESS_KEY",
            "secret_key": f"{source.credential_env_prefix}_SECRET_KEY",
            "session_token": f"{source.credential_env_prefix}_SESSION_TOKEN",
        },
        "sync_sets": [
            {"local": "checkpoints/", "remote": "checkpoints/"},
            {"local": "logs/", "remote": "logs/"},
            {"local": "configs/", "remote": "configs/"},
        ],
        "checkpoint_handoff": {
            "schema_version": CHECKPOINT_MANIFEST_VERSION,
            "manifest_prefix": f"{run_prefix}manifests/checkpoints/",
            "write_order": ["checkpoint_objects", "manifest_object", "callback"],
            "callback_url": callback_url,
            "authorization": {
                "scheme": "Basic",
                "api_key_environment": "MODELFICHE_HANDOFF_TOKEN",
            },
        },
        "policy": request.backup_policy,
    }


def _request_identity(request: LaunchRequest) -> dict[str, object]:
    value = asdict(request)
    value.pop("profile_id", None)
    value.pop("base_url", None)
    return value


def _launch_projection(launch: models.TrainingLaunch) -> dict[str, object]:
    return {
        "id": launch.id,
        "run_id": launch.run_id,
        "project_id": launch.project_id,
        "dataset_version_id": launch.dataset_version_id,
        "state": launch.state,
        "client_request_id": launch.client_request_id,
        "manifest_version": launch.manifest_version,
        "manifest_digest": launch.manifest_digest,
        "manifest": launch.redacted_manifest,
        "dataset_export_job_id": launch.dataset_export_job_id,
        "dataset_export_asset_id": launch.dataset_export_asset_id,
        "source_id": launch.source_id,
        "source_fingerprint": launch.source_fingerprint,
        "run_prefix": launch.run_prefix,
        "backup_status": launch.backup_status,
        "checkpoint_handoff_status": launch.checkpoint_handoff_status,
        "created_at": launch.created_at,
        "updated_at": launch.updated_at,
    }


def get_launch_projection(launch: models.TrainingLaunch) -> dict[str, object]:
    return _launch_projection(launch)

def _check(code: str, passed: bool, message: str, *, fix: str | None = None, owner: str = "model_fiche") -> dict[str, object]:
    return {
        "code": code,
        "status": "pass" if passed else "blocked",
        "message": message,
        "owner": owner,
        "fix": fix,
    }


def launch_preflight(session: Session, launch: models.TrainingLaunch) -> dict[str, object]:
    run = session.get(models.TrainingRun, launch.run_id)
    version = session.get(models.DatasetVersion, launch.dataset_version_id)
    credential = session.get(models.WandbIngestCredential, launch.wandb_credential_id)
    source = session.get(models.ImportSource, launch.source_id) if launch.source_id else None
    export = session.get(models.Job, launch.dataset_export_job_id) if launch.dataset_export_job_id else None
    item_count = int(
        session.scalar(
            select(func.count())
            .select_from(models.DatasetItem)
            .where(
                models.DatasetItem.dataset_version_id == launch.dataset_version_id,
                models.DatasetItem.included.is_(True),
            )
        )
        or 0
    )
    export_state = export.state.value if export is not None and hasattr(export.state, "value") else str(export.state if export else "")
    export_path = Path(str((export.result or {}).get("path", ""))).resolve() if export and (export.result or {}).get("path") else None
    export_ready = bool(export and export_state == "succeeded" and export_path and export_path.is_file())
    telemetry = (launch.redacted_manifest or {}).get("telemetry", {})
    public_url = str(telemetry.get("base_url") or "")
    claims = telemetry.get("token", {}).get("claims", {}) if isinstance(telemetry, dict) else {}
    expires_text = str(claims.get("expires_at") or "")
    try:
        expires_at = datetime.fromisoformat(expires_text.replace("Z", "+00:00"))
        token_window_valid = expires_at > datetime.now(timezone.utc)
    except ValueError:
        token_window_valid = False
    launch_open = launch.state not in {"canceled", "deleted", "archived"}
    run_pending = run is not None and run.status == "pending" and run.archived_at is None

    checks = [
        _check("LAUNCH_OPEN", launch_open, "Launch can accept a trainer." if launch_open else "Launch is no longer startable."),
        _check("RUN_PENDING", run_pending, "DAM training record is pending." if run_pending else "DAM training record is not pending."),
        _check("DATASET_PUBLISHED", bool(version and version.status == "published"), "Dataset version is immutable and published." if version and version.status == "published" else "Publish the selected dataset version.", fix="Publish the dataset version."),
        _check("DATASET_NONEMPTY", item_count > 0, f"{item_count} included training images found." if item_count else "The published version has no included images.", fix="Include at least one image and publish a new version."),
        _check("EXPORT_READY", export_ready, f"Dataset export is ready ({export_path.stat().st_size} bytes)." if export_ready and export_path else "Dataset export is not complete or is missing.", fix="Wait for or retry the dataset export."),
        _check("SIGNING_CREDENTIAL", bool(credential and credential.state == "active"), "Run-scoped signing credential is active." if credential and credential.state == "active" else "The W&B signing credential is missing, revoked, or inactive.", fix="Run `mfiche training setup`."),
        _check("SIGNING_WINDOW", token_window_valid, "Signing claims are within their validity window." if token_window_valid else "The launch signing window has expired.", fix="Create a fresh launch."),
        _check("STORAGE_SOURCE", bool(source and source.is_active and source.identity_fingerprint), "Verified training storage is bound." if source and source.is_active and source.identity_fingerprint else "A verified writable training source is required.", fix="Verify a training S3 source."),
        _check("PUBLIC_INGRESS", public_url.startswith("https://"), f"Public W&B ingress is configured at {public_url}." if public_url.startswith("https://") else "A public HTTPS W&B ingress is required.", fix="Configure TITLES_WANDB_INGRESS_BASE_URL and verify the tunnel.", owner="operator"),
    ]
    blockers = [item for item in checks if item["status"] != "pass"]
    ready = not blockers
    next_action = None if ready else {
        "label": blockers[0]["message"],
        "fix": blockers[0].get("fix"),
        "owner": blockers[0]["owner"],
    }
    phase = "ready_for_trainer" if ready else (
        "waiting_for_export" if any(item["code"] == "EXPORT_READY" for item in blockers) else "blocked"
    )
    return {
        "launch_id": launch.id,
        "run_id": launch.run_id,
        "ready_to_start": ready,
        "phase": phase,
        "checks": checks,
        "blockers": blockers,
        "next_action": next_action,
        "safe_to_retry": launch_open and run_pending,
    }


def launch_packet(session: Session, launch: models.TrainingLaunch) -> dict[str, object]:
    preflight = launch_preflight(session, launch)
    if not preflight["ready_to_start"]:
        raise LaunchError("training launch preflight has blockers")
    manifest = launch.redacted_manifest
    telemetry = manifest["telemetry"]
    return {
        "schema_version": "modelfiche.training-packet.v1",
        "launch_id": launch.id,
        "run_id": launch.run_id,
        "manifest": manifest,
        "preflight": preflight,
        "ai_toolkit": {
            "logging": {
                "use_wandb": True,
                "project_name": telemetry["project"],
                "run_name": manifest["run"]["name"],
            },
            "required_environment": [
                "WANDB_BASE_URL",
                "WANDB_PROJECT",
                "WANDB_RUN_ID",
                "WANDB_RESUME",
                "WANDB_API_KEY",
                "MODELFICHE_HANDOFF_TOKEN",
            ],
        },
        "instructions": [
            "Render trainer.env with `mfiche training run prepare`.",
            "Download and verify the dataset URL and manifest checksum.",
            "Source trainer.env only inside the trainer process.",
            "Start AI Toolkit with the generated logging configuration.",
        ],
    }


def cancel_training_launch(session: Session, launch: models.TrainingLaunch) -> None:
    run = session.get(models.TrainingRun, launch.run_id)
    if run is None or run.status not in {"pending", "running"}:
        raise LaunchError("only pending or running training launches can be canceled")
    launch.state = "canceled"
    run.status = "canceled"
    run.finished_at = models.utcnow()
    session.flush()


def delete_unstarted_training_launch(session: Session, launch: models.TrainingLaunch) -> None:
    run = session.get(models.TrainingRun, launch.run_id)
    if run is None or run.status not in {"pending", "canceled"}:
        raise LaunchError("only unstarted training launches can be deleted")
    dependent_models = (
        models.TrainingStage,
        models.TrainingMetric,
        models.RunMetricSummary,
        models.RunEvent,
        models.RunUpload,
        models.RunArtifactHandoff,
        models.Checkpoint,
        models.Sample,
    )
    if any(
        int(session.scalar(select(func.count()).select_from(model).where(model.run_id == run.id)) or 0)
        for model in dependent_models
    ):
        raise LaunchError("launch has accepted training data and must be archived instead")
    export = session.get(models.Job, launch.dataset_export_job_id) if launch.dataset_export_job_id else None
    if export is not None:
        raw_path = (export.result or {}).get("path")
        if raw_path:
            root = get_settings().export_root.resolve()
            path = Path(str(raw_path)).resolve()
            if root in path.parents and path.is_file():
                path.unlink()
        session.delete(export)
    session.delete(launch)
    session.delete(run)
    session.flush()


def create_training_launch(session: Session, request: LaunchRequest) -> tuple[models.TrainingLaunch, bool]:
    request_digest = _sha256(_request_identity(request))
    existing = session.scalar(
        select(models.TrainingLaunch).where(
            models.TrainingLaunch.workspace_id == request.workspace_id,
            models.TrainingLaunch.client_request_id == request.client_request_id,
        )
    )
    if existing is not None:
        if existing.redacted_manifest.get("request_digest") != request_digest:
            raise LaunchError("client_request_id is already bound to a different launch request")
        return existing, False

    version = session.get(models.DatasetVersion, request.dataset_version_id)
    if version is None:
        raise LaunchError("dataset version was not found")
    dataset = session.get(models.Dataset, version.dataset_id)
    if dataset is None:
        raise LaunchError("dataset was not found")
    project = session.get(models.Project, dataset.project_id)
    if project is None or project.workspace_id != request.workspace_id:
        raise LaunchError("dataset version is outside the active workspace")
    if version.status != "published":
        raise LaunchError("training launches require a published dataset version")

    credential = session.get(models.WandbIngestCredential, request.credential_id)
    if credential is None or credential.workspace_id != request.workspace_id:
        raise LaunchError("W&B credential was not found")
    if credential.state != "active":
        raise LaunchError("W&B credential is not active")
    if credential.project_id is not None and credential.project_id != project.id:
        raise LaunchError("W&B credential is not scoped to this project")

    source = session.get(models.ImportSource, request.source_id)
    if source is None or source.workspace_id != request.workspace_id:
        raise LaunchError("S3 source was not found")
    if source.provider != "s3" or not source.is_active:
        raise LaunchError("training launches require an active S3 source")
    if not source.identity_fingerprint:
        raise LaunchError("S3 source must be verified before training launch")

    available_endpoints = {adapter.endpoint_id for adapter in list_adapters()}
    unsupported = sorted(set(request.supported_endpoint_ids) - available_endpoints)
    if unsupported:
        raise LaunchError(f"unsupported FAL endpoint IDs: {', '.join(unsupported)}")

    if not version.content_digest:
        items = session.scalars(
            select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)
        ).all()
        version.content_digest = content_digest(version, items)

    run = models.TrainingRun(
        project_id=project.id,
        dataset_version_id=version.id,
        name=request.name.strip(),
        trainer=request.trainer,
        base_model=request.base_model.strip(),
        status="pending",
        normalized_config={
            "output_directory": request.output_directory,
            "checkpoint_policy": request.checkpoint_policy,
            "backup_policy": request.backup_policy,
        },
        wandb_run_id=_allocate_wandb_run_id(session),
        wandb_project=_project_slug(project.title, project.id),
        wandb_display_name=request.name.strip(),
        wandb_sdk_version=WANDB_SDK_VERSION,
        wandb_protocol_revision=WANDB_PROTOCOL_REVISION,
    )
    session.add(run)
    session.flush()

    run_prefix = f"projects/{project.id}/runs/{run.id}/"
    if not _source_allows_prefix(source, run_prefix):
        raise LaunchError("S3 source does not permit the required run prefix")
    run.source_prefix = run_prefix
    run.origin_source_id = source.id
    run.origin_source_fingerprint = source.identity_fingerprint

    export_job = models.Job(
        workspace_id=request.workspace_id,
        kind="export.package",
        profile_id=request.profile_id,
        idempotency_key=f"training-launch-export:{request.workspace_id}:{request.client_request_id}",
        payload={
            "name": f"Training dataset for {request.name.strip()}",
            "dataset_version_ids": [version.id],
            "training_launch": True,
        },
    )
    session.add(export_job)
    session.flush()

    now = models.utcnow()
    expires_at = now + timedelta(seconds=request.expected_duration_seconds)
    launch = models.TrainingLaunch(
        workspace_id=request.workspace_id,
        project_id=project.id,
        run_id=run.id,
        dataset_version_id=version.id,
        wandb_credential_id=credential.id,
        state="preparing",
        client_request_id=request.client_request_id,
        manifest_version=MANIFEST_VERSION,
        dataset_export_job_id=export_job.id,
        source_id=source.id,
        source_fingerprint=source.identity_fingerprint,
        run_prefix=run_prefix,
        backup_policy=request.backup_policy,
        checkpoint_policy=request.checkpoint_policy,
        supported_endpoint_ids=sorted(set(request.supported_endpoint_ids)),
        credential_bound_at=now,
        credential_validated_at=now,
        backup_status="pending",
        checkpoint_handoff_status="awaiting_manifest",
    )
    session.add(launch)
    session.flush()

    base_url = request.base_url.rstrip("/")
    claims = {
        "claims_version": 1,
        "credential_id": credential.id,
        "kid": credential.key_id,
        "workspace_id": request.workspace_id,
        "project_id": project.id,
        "launch_id": launch.id,
        "run_id": run.id,
        "wandb_run_id": run.wandb_run_id,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "launch_id": launch.id,
        "request_digest": request_digest,
        "run": {
            "id": run.id,
            "name": run.name,
            "project_id": project.id,
            "dataset_version_id": version.id,
            "trainer": run.trainer,
            "base_model": run.base_model,
        },
        "dataset": {
            "export_id": export_job.id,
            "sha256": version.content_digest,
            "size": None,
            "download_capability_required": True,
            "download_url": f"{base_url}/api/transfers/{export_job.id}/download",
        },
        "telemetry": {
            "sdk": "wandb",
            "sdk_version": WANDB_SDK_VERSION,
            "base_url": base_url,
            "entity": "dam",
            "project": run.wandb_project,
            "run_id": run.wandb_run_id,
            "resume": "allow",
            "api_key_environment": "WANDB_API_KEY",
            "credential_alias": credential.alias,
            "token": {
                "format": TOKEN_FORMAT,
                "kid": credential.key_id,
                "canonicalization": "json-sort-keys-compact-utf8",
                "claims": claims,
            },
        },
        "backup": _backup_configuration(
            request,
            source,
            run_prefix,
            f"{base_url}/checkpoint-handoffs/{run.id}",
        ),
        "artifact_handoff": {
            "schema_version": CHECKPOINT_MANIFEST_VERSION,
            "checkpoint_prefix": f"{run_prefix}checkpoints/",
            "manifest_prefix": f"{run_prefix}manifests/checkpoints/",
            "manifest_callback_url": f"{base_url}/checkpoint-handoffs/{run.id}",
            "manifest_token_environment": "MODELFICHE_HANDOFF_TOKEN",
            "checkpoint_policy": request.checkpoint_policy,
            "supported_endpoint_ids": launch.supported_endpoint_ids,
        },
    }
    launch.redacted_manifest = manifest
    launch.manifest_digest = _sha256(manifest)
    credential.last_used_at = now
    session.flush()
    return launch, True


def launch_environment(launch: models.TrainingLaunch) -> str:
    telemetry = launch.redacted_manifest["telemetry"]
    claims = telemetry["token"]["claims"]
    claims_json = _canonical_json(claims).decode("utf-8")
    lines = [
        f"export WANDB_BASE_URL={json.dumps(telemetry['base_url'])}",
        f"export WANDB_PROJECT={json.dumps(telemetry['project'])}",
        f"export WANDB_RUN_ID={json.dumps(telemetry['run_id'])}",
        'export WANDB_RESUME="allow"',
        f"# Sign these canonical claims with credential alias {telemetry['credential_alias']!r}:",
        f"# {claims_json}",
        'export WANDB_API_KEY="<MF1_BASE32_ED25519_SIGNED_TOKEN>"',
        'export MODELFICHE_HANDOFF_TOKEN="$WANDB_API_KEY"',
    ]
    return "\n".join(lines) + "\n"
