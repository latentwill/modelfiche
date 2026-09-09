from __future__ import annotations

from contextvars import ContextVar
import json
from typing import Any, Mapping

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

_workspace_id: ContextVar[str | None] = ContextVar("workspace_id", default=None)
_request_method: ContextVar[str] = ContextVar("request_method", default="GET")


class WorkspaceContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        selected = headers.get(b"x-workspace-id")
        method = str(scope.get("method") or "GET").upper()
        if method in {"POST", "PUT", "PATCH", "DELETE"} and str(scope.get("path") or "").startswith("/api/") and str(scope.get("path") or "") != "/api/remote-session" and not selected:
            await _reject_workspace_request(send, 428, {
                "code": "workspace_required",
                "message": "X-Workspace-ID is required for API mutations",
            })
            return
        workspace_token = _workspace_id.set(selected.decode("utf-8") if selected else None)
        method_token = _request_method.set(method)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_method.reset(method_token)
            _workspace_id.reset(workspace_token)

async def _reject_workspace_request(send, status: int, detail: Mapping[str, Any]) -> None:
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
    })
    await send({"type": "http.response.body", "body": body})



def get_or_404(db: Session, model, object_id: str):
    value = db.get(model, object_id)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    actual_workspace_id = entity_workspace_id(db, value)
    selected_reference = _workspace_id.get()
    selected_workspace = current_workspace(db) if selected_reference else None
    selected_workspace_id = str(selected_workspace.id) if selected_workspace else None
    if (
        actual_workspace_id
        and selected_workspace_id
        and actual_workspace_id != selected_workspace_id
    ):
        if _request_method.get() in {"POST", "PUT", "PATCH", "DELETE"}:
            raise HTTPException(status_code=409, detail={
                "code": "workspace_entity_mismatch",
                "message": f"{model.__name__} belongs to another workspace",
                "declared_workspace_id": selected_workspace_id,
                "actual_workspace_id": actual_workspace_id,
                "entity_type": model.__name__,
                "entity_id": str(object_id),
            })
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return value


def workspace_object(db: Session, model, object_id: str):
    value = get_or_404(db, model, object_id)
    workspace_id = entity_workspace_id(db, value)
    if workspace_id is not None and workspace_id != current_workspace(db).id:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return value


def current_workspace(db: Session) -> models.Workspace:
    selected_reference = _workspace_id.get()
    workspace = (
        db.scalar(
            select(models.Workspace).where(
                (models.Workspace.id == selected_reference)
                | (models.Workspace.slug == selected_reference)
            )
        )
        if selected_reference
        else db.scalar(select(models.Workspace).order_by(models.Workspace.created_at))
    )
    if workspace is None:
        if selected_reference:
            raise HTTPException(status_code=404, detail="workspace not found")
        raise HTTPException(status_code=503, detail="workspace is not initialized")
    return workspace


def entity_workspace_id(db: Session, value: Any) -> str | None:
    """Resolve an entity's owning workspace without ambient request state."""
    direct = getattr(value, "workspace_id", None)
    if direct:
        return str(direct)
    if isinstance(value, models.Workspace):
        return str(value.id)
    project_id = getattr(value, "project_id", None)
    if project_id:
        project = db.get(models.Project, str(project_id))
        return str(project.workspace_id) if project else None
    if isinstance(value, (models.DatasetVersion, models.DatasetDraft)):
        dataset = db.get(models.Dataset, str(value.dataset_id))
        return entity_workspace_id(db, dataset) if dataset else None
    if isinstance(value, models.TrainingRun):
        project = db.get(models.Project, str(value.project_id))
        return str(project.workspace_id) if project else None
    if isinstance(value, models.Checkpoint):
        run = db.get(models.TrainingRun, str(value.run_id))
        return entity_workspace_id(db, run) if run else None
    if isinstance(value, models.ModelVersion):
        model = db.get(models.Model, str(value.model_id))
        return entity_workspace_id(db, model) if model else None
    if isinstance(value, models.EvalRun):
        definition = db.get(models.EvalDefinition, str(value.definition_id))
        return entity_workspace_id(db, definition) if definition else None
    return None


_RESOLVABLE_ENTITY_MODELS = {
    "asset": models.Asset,
    "checkpoint": models.Checkpoint,
    "dataset": models.Dataset,
    "dataset-version": models.DatasetVersion,
    "eval-run": models.EvalRun,
    "grid": models.GridDefinition,
    "job": models.Job,
    "model": models.Model,
    "model-version": models.ModelVersion,
    "project": models.Project,
    "run": models.TrainingRun,
    "training-launch": models.TrainingLaunch,
}


def resolve_entity_workspace(db: Session, entity_type: str, entity_id: str) -> models.Workspace:
    model = _RESOLVABLE_ENTITY_MODELS.get(entity_type)
    if model is None:
        raise HTTPException(status_code=404, detail="entity type is not workspace-resolvable")
    value = db.get(model, entity_id)
    workspace_id = entity_workspace_id(db, value) if value is not None else None
    workspace = db.get(models.Workspace, workspace_id) if workspace_id else None
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"{entity_type} not found")
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
