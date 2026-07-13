from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..wandb_credentials import (
    CredentialError,
    CredentialRegistration,
    credential_projection,
    register_credential,
    revoke_credential,
    rotate_credential,
)


router = APIRouter(prefix="/integrations/wandb")
DB = Annotated[Session, Depends(get_db)]


def _workspace_credential(db: Session, credential_id: str) -> models.WandbIngestCredential:
    credential = get_or_404(db, models.WandbIngestCredential, credential_id)
    if credential.workspace_id != current_workspace(db).id:
        raise HTTPException(status_code=404, detail="W&B credential not found")
    return credential


@router.get("/credentials")
def list_credentials(db: DB, project_id: str | None = None):
    workspace = current_workspace(db)
    query = select(models.WandbIngestCredential).where(
        models.WandbIngestCredential.workspace_id == workspace.id
    )
    if project_id is not None:
        query = query.where(models.WandbIngestCredential.project_id.in_((None, project_id)))
    rows = db.scalars(query.order_by(models.WandbIngestCredential.alias)).all()
    return [credential_projection(row) for row in rows]


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
def create_credential(
    body: schemas.WandbCredentialCreate,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    workspace = current_workspace(db)
    actor = active_profile(db, x_profile_id)
    try:
        credential = register_credential(
            db,
            CredentialRegistration(
                workspace_id=workspace.id,
                project_id=body.project_id,
                alias=body.alias,
                public_key=body.public_key,
            ),
        )
    except CredentialError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_activity(
        db,
        action="wandb_credential.created",
        subject_type="wandb_ingest_credential",
        subject_id=credential.id,
        profile_id=actor.id,
        project_id=credential.project_id,
        details={"alias": credential.alias, "key_id": credential.key_id, "scope": "project" if credential.project_id else "workspace"},
    )
    db.commit()
    return credential_projection(credential)


@router.post("/credentials/{credential_id}/rotate")
def rotate(
    credential_id: str,
    body: schemas.WandbCredentialRotate,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    credential = _workspace_credential(db, credential_id)
    try:
        rotate_credential(db, credential, body.public_key)
    except CredentialError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_activity(
        db,
        action="wandb_credential.rotated",
        subject_type="wandb_ingest_credential",
        subject_id=credential.id,
        profile_id=active_profile(db, x_profile_id).id,
        project_id=credential.project_id,
        details={"alias": credential.alias, "key_id": credential.key_id},
    )
    db.commit()
    return credential_projection(credential)


@router.post("/credentials/{credential_id}/revoke")
def revoke(
    credential_id: str,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    credential = _workspace_credential(db, credential_id)
    revoke_credential(db, credential)
    record_activity(
        db,
        action="wandb_credential.revoked",
        subject_type="wandb_ingest_credential",
        subject_id=credential.id,
        profile_id=active_profile(db, x_profile_id).id,
        project_id=credential.project_id,
        details={"alias": credential.alias, "key_id": credential.key_id},
    )
    db.commit()
    return credential_projection(credential)
