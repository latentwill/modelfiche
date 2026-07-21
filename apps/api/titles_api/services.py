from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
_workspace_id: ContextVar[str | None] = ContextVar("workspace_id", default=None)


class WorkspaceContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        selected = headers.get(b"x-workspace-id")
        token = _workspace_id.set(selected.decode("utf-8") if selected else None)
        try:
            await self.app(scope, receive, send)
        finally:
            _workspace_id.reset(token)



def get_or_404(db: Session, model, object_id: str):
    value = db.get(model, object_id)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return value


def workspace_object(db: Session, model, object_id: str):
    value = get_or_404(db, model, object_id)
    workspace_id = getattr(value, "workspace_id", None)
    if workspace_id is not None and workspace_id != current_workspace(db).id:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return value


def current_workspace(db: Session) -> models.Workspace:
    selected_id = _workspace_id.get()
    workspace = db.get(models.Workspace, selected_id) if selected_id else db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
    if workspace is None:
        if selected_id:
            raise HTTPException(status_code=404, detail="workspace not found")
        raise HTTPException(status_code=503, detail="workspace is not initialized")
    return workspace


def active_profile(db: Session, requested_id: str | None = None) -> models.UserProfile:
    workspace_id = current_workspace(db).id
    profile_id = requested_id
    if profile_id is None:
        pref = db.scalar(select(models.LocalPreference).where(models.LocalPreference.key == "active_profile"))
        profile_id = pref.value.get("profile_id") if pref else None
    profile = db.get(models.UserProfile, profile_id) if profile_id else None
    if profile is None or not profile.is_active or profile.workspace_id != workspace_id:
        profile = db.scalar(select(models.UserProfile).where(
            models.UserProfile.workspace_id == workspace_id,
            models.UserProfile.is_active.is_(True),
        ).order_by(models.UserProfile.created_at))
    if profile is None:
        raise HTTPException(status_code=409, detail="an active profile is required for this workspace")
    return profile



def apply_generation_prompt_settings(db: Session, prompt: str) -> str:
    """Apply the workspace-wide prompt affixes at the generation boundary."""
    preference = db.scalar(select(models.LocalPreference).where(models.LocalPreference.key == "operator_settings"))
    generation = preference.value.get("generation", {}) if preference and isinstance(preference.value, dict) else {}
    prepend = str(generation.get("prompt_prepend") or "").strip()
    append = str(generation.get("prompt_append") or "").strip()
    result = " ".join(part for part in (prepend, prompt.strip(), append) if part)
    if len(result) > 64_000:
        raise HTTPException(status_code=422, detail="prompt plus configured prepend/append exceeds 64000 characters")
    return result

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
