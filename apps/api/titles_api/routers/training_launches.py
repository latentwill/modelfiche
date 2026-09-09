from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..settings import get_settings
from ..training_launches import (
    LaunchError,
    LaunchRequest,
    cancel_training_launch,
    create_training_launch,
    delete_unstarted_training_launch,
    get_launch_projection,
    launch_environment,
    launch_packet,
    launch_preflight,
    rehome_training_launch,
)


router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def _workspace_launch(db: Session, launch_id: str) -> models.TrainingLaunch:
    launch = get_or_404(db, models.TrainingLaunch, launch_id)
    if launch.workspace_id != current_workspace(db).id:
        raise HTTPException(status_code=404, detail="training launch not found")
    return launch


@router.post("/training-launches/{launch_id}/rehome")
def rehome_launch(
    launch_id: str,
    body: schemas.TrainingRunRehome,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    launch = _workspace_launch(db, launch_id)
    actor = active_profile(db, x_profile_id)
    try:
        run = rehome_training_launch(
            db,
            launch,
            target_project_id=body.target_project_id,
            profile_id=actor.id,
            reason=body.reason,
        )
    except LaunchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_activity(
        db,
        action="training_run.rehomed",
        subject_type="training_run",
        subject_id=run.id,
        profile_id=actor.id,
        project_id=run.project_id,
        details={"launch_id": launch.id, "placement_epoch": run.placement_epoch},
    )
    db.commit()
    return {
        "launch": get_launch_projection(launch),
        "run_id": run.id,
        "project_id": run.project_id,
        "placement_epoch": run.placement_epoch,
    }



@router.get("/training-setup")
def training_setup(db: DB):
    workspace = current_workspace(db)
    credentials = db.scalars(
        select(models.WandbIngestCredential)
        .where(models.WandbIngestCredential.state == "active")
        .order_by(
            models.WandbIngestCredential.created_at,
            models.WandbIngestCredential.id,
        )
    ).all()
    sources = db.scalars(
        select(models.ImportSource)
        .where(
            models.ImportSource.workspace_id == workspace.id,
            models.ImportSource.provider == "s3",
            models.ImportSource.is_active.is_(True),
        )
        .order_by(models.ImportSource.name)
    ).all()
    ingress = get_settings().wandb_ingress_base_url
    checks = [
        {
            "code": "SIGNING_CREDENTIAL",
            "status": "pass" if credentials else "blocked",
            "message": "The global W&B signing key is active." if credentials else "The global W&B signing key is not authenticated.",
            "fix": None if credentials else "Run `mfiche training setup`.",
        },
        {
            "code": "TRAINING_STORAGE",
            "status": "pass" if sources else "blocked",
            "message": f"{len(sources)} active training source(s)." if sources else "No active training storage source.",
            "fix": None if sources else "Create an active S3 source.",
        },
        {
            "code": "TRAINER_TRANSPORT",
            "status": "pass" if sources else "blocked",
            "message": "Training uses outbound object storage; no inbound tunnel is required." if sources else "Connect an active S3 source.",
            "fix": None if sources else "Create an active S3 source.",
        },
        {
            "code": "LIVE_TELEMETRY",
            "status": "pass" if ingress else "blocked",
            "message": "W&B ingress is configured; verify reachability from the trainer."
            if ingress else "Live loss graphs and samples have no configured W&B ingress.",
            "fix": None if ingress else "Set TITLES_WANDB_INGRESS_BASE_URL and route SDK endpoints to the ingress; use --offline only for archival-only training.",
        },
    ]
    return {
        "ready": all(item["status"] == "pass" for item in checks),
        "offline_ready": all(item["status"] == "pass" for item in checks if item["code"] != "LIVE_TELEMETRY"),
        "checks": checks,
        "credentials": [
            {"id": item.id, "alias": item.alias, "scope": "global", "key_id": item.key_id}
            for item in credentials
        ],
        "sources": [
            {
                "id": item.id,
                "name": item.name,
                "bucket": item.bucket,
                "available": True,
            }
            for item in sources
        ],
        "ingress_base_url": ingress,
    }


def _resolve_live_telemetry(requested: bool | None, configured_ingress: str | None) -> bool:
    if requested is False:
        return False
    if not configured_ingress:
        raise HTTPException(status_code=409, detail={
            "code": "training_telemetry_not_configured",
            "message": "Live loss graphs and samples require TITLES_WANDB_INGRESS_BASE_URL. Configure reachable W&B ingress, or explicitly select live_telemetry=false (--offline) for archival-only training.",
        })
    return True


@router.post("/training-launches", status_code=status.HTTP_201_CREATED)
def create_launch(
    body: schemas.TrainingLaunchCreate,
    request: Request,
    response: Response,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    workspace = current_workspace(db)
    actor = active_profile(db, x_profile_id)
    get_or_404(db, models.DatasetVersion, body.dataset_version_id)
    if body.source_id:
        get_or_404(db, models.ImportSource, body.source_id)
    configured_ingress = get_settings().wandb_ingress_base_url
    live_telemetry = _resolve_live_telemetry(body.live_telemetry, configured_ingress)
    ingress_base_url = configured_ingress or str(request.base_url).rstrip("/")
    try:
        launch, created = create_training_launch(
            db,
            LaunchRequest(
                workspace_id=workspace.id,
                profile_id=actor.id,
                dataset_version_id=body.dataset_version_id,
                source_id=body.source_id,
                client_request_id=body.client_request_id,
                name=body.name,
                trainer=body.trainer,
                base_model=body.base_model,
                output_directory=body.output_directory,
                training_config=(
                    body.training_config.model_dump()
                    if body.training_config is not None
                    else None
                ),
                checkpoint_policy=body.checkpoint_policy,
                backup_policy=body.backup_policy,
                supported_endpoint_ids=body.supported_endpoint_ids,
                expected_duration_seconds=body.expected_duration_seconds,
                base_url=ingress_base_url,
                live_telemetry=live_telemetry,
            ),
        )
    except LaunchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        record_activity(
            db,
            action="training_launch.created",
            subject_type="training_launch",
            subject_id=launch.id,
            profile_id=actor.id,
            project_id=launch.project_id,
            details={
                "run_id": launch.run_id,
                "dataset_version_id": launch.dataset_version_id,
                "dataset_export_job_id": launch.dataset_export_job_id,
                "manifest_digest": launch.manifest_digest,
            },
        )
    else:
        response.status_code = status.HTTP_200_OK
    db.commit()
    return get_launch_projection(launch)


@router.get("/training-launches/{launch_id}")
def training_launch(launch_id: str, db: DB):
    return get_launch_projection(_workspace_launch(db, launch_id))


@router.get("/training-launches/{launch_id}/preflight")
def training_launch_preflight(launch_id: str, db: DB):
    return launch_preflight(db, _workspace_launch(db, launch_id))


@router.get("/training-launches/{launch_id}/packet")
def training_launch_packet(launch_id: str, db: DB):
    launch = _workspace_launch(db, launch_id)
    try:
        return launch_packet(db, launch)
    except LaunchError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TRAINING_PREFLIGHT_BLOCKED",
                "message": str(exc),
                "preflight": launch_preflight(db, launch),
            },
        ) from exc


@router.post("/training-launches/{launch_id}/cancel")
def cancel_launch(
    launch_id: str,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    launch = _workspace_launch(db, launch_id)
    try:
        cancel_training_launch(db, launch)
    except LaunchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_activity(
        db,
        action="training_launch.canceled",
        subject_type="training_launch",
        subject_id=launch.id,
        profile_id=active_profile(db, x_profile_id).id,
        project_id=launch.project_id,
        details={"run_id": launch.run_id},
    )
    db.commit()
    return get_launch_projection(launch)


@router.delete("/training-launches/{launch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_launch(launch_id: str, db: DB):
    launch = _workspace_launch(db, launch_id)
    try:
        delete_unstarted_training_launch(db, launch)
    except LaunchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/training-launches/{launch_id}/manifest")
def training_launch_manifest(launch_id: str, db: DB):
    launch = _workspace_launch(db, launch_id)
    payload = json.dumps(
        launch.redacted_manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="training-launch-{launch.id}.json"'},
    )


@router.get("/training-launches/{launch_id}/environment", response_class=PlainTextResponse)
def training_launch_environment(launch_id: str, db: DB):
    return launch_environment(_workspace_launch(db, launch_id))
