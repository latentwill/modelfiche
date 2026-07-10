from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


def get_or_404(db: Session, model, object_id: str):
    value = db.get(model, object_id)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return value


def current_workspace(db: Session) -> models.Workspace:
    workspace = db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
    if workspace is None:
        raise HTTPException(status_code=503, detail="workspace is not initialized")
    return workspace


def active_profile(db: Session, requested_id: str | None = None) -> models.UserProfile:
    profile_id = requested_id
    if profile_id is None:
        pref = db.scalar(select(models.LocalPreference).where(models.LocalPreference.key == "active_profile"))
        profile_id = pref.value.get("profile_id") if pref else None
    profile = db.get(models.UserProfile, profile_id) if profile_id else None
    if profile is None or not profile.is_active:
        profile = db.scalar(select(models.UserProfile).where(models.UserProfile.is_active.is_(True)).order_by(models.UserProfile.created_at))
    if profile is None:
        raise HTTPException(status_code=409, detail="an active profile is required")
    return profile


def record_activity(
    db: Session,
    *,
    action: str,
    subject_type: str,
    subject_id: str,
    profile_id: str | None,
    workspace_id: str | None = None,
    project_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> models.ActivityEvent:
    event = models.ActivityEvent(
        workspace_id=workspace_id or current_workspace(db).id,
        project_id=project_id,
        profile_id=profile_id,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        details=details or {},
    )
    db.add(event)
    return event
