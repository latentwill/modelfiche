from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..settings import get_settings
from ..training_launches import (
    LaunchError,
    LaunchRequest,
    create_training_launch,
    get_launch_projection,
    launch_environment,
)


router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def _workspace_launch(db: Session, launch_id: str) -> models.TrainingLaunch:
    launch = get_or_404(db, models.TrainingLaunch, launch_id)
    if launch.workspace_id != current_workspace(db).id:
        raise HTTPException(status_code=404, detail="training launch not found")
    return launch


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
    ingress_base_url = get_settings().wandb_ingress_base_url or str(request.base_url).rstrip("/")
    try:
        launch, created = create_training_launch(
            db,
            LaunchRequest(
                workspace_id=workspace.id,
                profile_id=actor.id,
                dataset_version_id=body.dataset_version_id,
                credential_id=body.wandb_credential_id,
                source_id=body.source_id,
                client_request_id=body.client_request_id,
                name=body.name,
                trainer=body.trainer,
                base_model=body.base_model,
                output_directory=body.output_directory,
                checkpoint_policy=body.checkpoint_policy,
                backup_policy=body.backup_policy,
                supported_endpoint_ids=body.supported_endpoint_ids,
                expected_duration_seconds=body.expected_duration_seconds,
                base_url=ingress_base_url,
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
