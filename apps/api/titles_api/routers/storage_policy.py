from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import current_workspace
from ..storage.contracts import StoragePolicyPatchV1, StoragePolicyV1
from .storage_migrations import router as migration_router
from ..storage.policy import patch_policy, project_policy
from ..storage.repository import PolicyConflict, StorageNotFound, StorageRepositoryError

router = APIRouter(tags=["storage"])
DB = Annotated[Session, Depends(get_db)]


def _begin_immediate(db: Session) -> None:
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        db.commit()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")

@router.get("/storage-policy", response_model=StoragePolicyV1)
def get_storage_policy(db: DB) -> StoragePolicyV1:
    workspace = current_workspace(db)
    return project_policy(db, workspace.id)


@router.patch("/storage-policy")
def update_storage_policy(body: StoragePolicyPatchV1, db: DB):
    workspace = current_workspace(db)
    try:
        _begin_immediate(db)
        current = project_policy(db, workspace.id)
        updated = patch_policy(db, workspace.id, body)
        db.commit()
    except (PolicyConflict, StorageNotFound, StorageRepositoryError) as exc:
        db.rollback()
        latest = project_policy(db, workspace.id)
        code = "storage_policy_conflict" if isinstance(exc, PolicyConflict) else "storage_policy_unavailable"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": code,
                "message": str(exc),
                "current": latest.model_dump(mode="json"),
            },
        ) from exc
    payload = updated.model_dump(mode="json")
    payload["announcement"] = {
        "event_id": f"storage-policy:{updated.version}",
        "mode": "assertive",
        "message": "Storage policy updated.",
        "focus_target_id": "storage_policy_status",
    }
    return payload


# app.py intentionally includes this router only; migrations are mounted here so
# the canonical routes remain available without widening the application cutover.
router.include_router(migration_router)
