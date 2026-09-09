from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import active_profile, get_or_404, record_activity
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


def _credential(db: Session, credential_id: str) -> models.WandbIngestCredential:
    return get_or_404(db, models.WandbIngestCredential, credential_id)


@router.get("/credentials")
def list_credentials(db: DB):
    rows = db.scalars(
        select(models.WandbIngestCredential).order_by(
            models.WandbIngestCredential.created_at,
            models.WandbIngestCredential.id,
        )
    ).all()
    return [credential_projection(row) for row in rows]


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
def create_credential(
    body: schemas.WandbCredentialCreate,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    actor = active_profile(db, x_profile_id)
    try:
        credential = register_credential(
            db,
            CredentialRegistration(
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
        project_id=None,
        details={"alias": credential.alias, "key_id": credential.key_id, "scope": "global"},
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
    credential = _credential(db, credential_id)
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
        project_id=None,
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
    credential = _credential(db, credential_id)
    revoke_credential(db, credential)
    record_activity(
        db,
        action="wandb_credential.revoked",
        subject_type="wandb_ingest_credential",
        subject_id=credential.id,
        profile_id=active_profile(db, x_profile_id).id,
        project_id=None,
        details={"alias": credential.alias, "key_id": credential.key_id},
    )
    db.commit()
    return credential_projection(credential)
