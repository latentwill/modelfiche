from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services import current_workspace, get_or_404
from .fal_admissions import BILLING_ACKNOWLEDGEMENT, FalAdmitRequestV1, admit_fal_admission

router = APIRouter(tags=["generation-queue"])
DB = Annotated[Session, Depends(get_db)]

QUEUE_CAPABILITIES = {
    "fal": {"owner": "dam", "dispatch": "internal"},
    # Contract only. A future adapter records external jobs, never re-schedules them here.
    "comfyui": {"owner": "provider", "dispatch": "provider_native"},
}

def queue_capability(provider: str) -> dict[str, str]:
    return QUEUE_CAPABILITIES.get(provider.lower(), {"owner": "provider", "dispatch": "unsupported"})

class QueueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: Literal["image", "eval", "grid"]
    provider: str
    context: dict[str, Any]
    model_version_ids: list[UUID] = Field(min_length=1)
    compiled_request_id: UUID
    admission_id: UUID | None = None
    client_request_id: UUID
    request: dict[str, Any]

class QueueStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    provider_job_id: str | None = None

def normalize_state(value: str) -> str:
    state = value.lower()
    if any(token in state for token in ("complete", "success", "succeed")): return "completed"
    if any(token in state for token in ("fail", "error", "cancel")): return "failed"
    if any(token in state for token in ("run", "process", "submit")): return "running"
    return "queued"

def projection(item: models.GenerationQueueItem) -> dict[str, Any]:
    return {"id": item.id, "workflow": item.workflow, "provider": item.provider, "queue_owner": item.queue_owner,
        "project_id": item.project_id, "model_id": item.model_id, "model_version_ids": item.model_version_ids,
        "compiled_request_id": item.compiled_request_id, "admission_id": item.admission_id, "client_request_id": item.client_request_id,
        "status": item.state, "progress": item.progress, "result": item.result, "error": item.error,
        "provider_job_id": item.provider_job_id, "job_id": item.job_id, "request": item.request_snapshot,
        "created_at": item.created_at, "updated_at": item.updated_at}

@router.get("/generation-queue/capabilities")
def capabilities():
    return QUEUE_CAPABILITIES

@router.get("/generation-queue")
def list_queue(db: DB, project_id: UUID | None = None, limit: int = Query(100, ge=1, le=500)):
    workspace = current_workspace(db)
    query = select(models.GenerationQueueItem).where(models.GenerationQueueItem.workspace_id == workspace.id)
    if project_id:
        query = query.where(models.GenerationQueueItem.project_id == str(project_id))
    # Stable FIFO display even when timestamps collide.
    items = db.scalars(query.order_by(models.GenerationQueueItem.created_at.asc(), models.GenerationQueueItem.id.asc()).limit(limit)).all()
    return {"items": [projection(item) for item in items]}

@router.get("/generation-queue/{item_id}")
def get_queue_item(item_id: str, db: DB):
    return projection(get_or_404(db, models.GenerationQueueItem, item_id))

@router.post("/generation-queue/claim")
def claim_next(db: DB):
    """Atomically claim and admit exactly one DAM-owned FIFO item; provider contact remains worker-owned."""
    workspace = current_workspace(db)
    item = db.scalar(select(models.GenerationQueueItem).where(models.GenerationQueueItem.workspace_id == workspace.id,
        models.GenerationQueueItem.queue_owner == "dam", models.GenerationQueueItem.state == "queued")
        .order_by(models.GenerationQueueItem.created_at.asc(), models.GenerationQueueItem.id.asc()).limit(1))
    if item is None: return None
    claimed = db.execute(update(models.GenerationQueueItem).where(models.GenerationQueueItem.id == item.id,
        models.GenerationQueueItem.state == "queued", models.GenerationQueueItem.queue_owner == "dam").values(state="running"))
    if claimed.rowcount != 1:
        db.rollback(); return None
    # Full binding validation prevents a compiled admission being attached to another context/model.
    admission = get_or_404(db, models.FalAdmission, item.admission_id)
    subject = db.scalar(select(models.FalSubject).where(models.FalSubject.admission_id == admission.id).order_by(models.FalSubject.ordinal))
    definition = dict(subject.definition or {}) if subject else {}
    if (admission.workspace_id != workspace.id or definition.get("project_id") != item.project_id
        or definition.get("model_version_id") not in item.model_version_ids
        or str(definition.get("admission_input_digest") or "") == ""):
        item.state = "failed"; item.error = "compiled admission does not match queued context/models"; db.commit(); return projection(item)
    db.flush()
    accepted = admit_fal_admission(admission.id, FalAdmitRequestV1(expected_version=admission.version,
        billing_acknowledgement=BILLING_ACKNOWLEDGEMENT), db)
    job = db.scalar(select(models.Job).where(models.Job.payload["admission_id"].as_string() == admission.id).order_by(models.Job.created_at.desc()))
    db.refresh(item); item.job_id = job.id if job else None; item.provider_job_id = admission.eval_run_id
    db.commit(); db.refresh(item)
    return projection(item)

@router.patch("/generation-queue/{item_id}")
def update_queue_item(item_id: str, body: QueueStateUpdate, db: DB):
    item = get_or_404(db, models.GenerationQueueItem, item_id)
    item.state = normalize_state(body.status)
    if body.progress is not None: item.progress = body.progress
    if body.result is not None: item.result = body.result
    item.error = body.error
    if body.provider_job_id is not None: item.provider_job_id = body.provider_job_id
    db.commit(); db.refresh(item)
    return projection(item)

@router.post("/generation-queue", status_code=status.HTTP_202_ACCEPTED)
def enqueue(body: QueueCreate, db: DB):
    workspace = current_workspace(db)
    existing = db.scalar(select(models.GenerationQueueItem).where(models.GenerationQueueItem.workspace_id == workspace.id, models.GenerationQueueItem.client_request_id == str(body.client_request_id)))
    if existing:
        if existing.compiled_request_id != str(body.compiled_request_id) or existing.request_snapshot != body.request:
            raise HTTPException(status_code=409, detail="client_request_id already identifies a different queued request")
        return projection(existing)
    capability = queue_capability(body.provider)
    if capability["dispatch"] == "provider_native":
        raise HTTPException(status_code=422, detail=f"{body.provider} owns its provider-native queue; DAM must reference an external job instead of scheduling it")
    if capability["dispatch"] != "internal":
        raise HTTPException(status_code=422, detail=f"generation provider is not installed: {body.provider}")
    if body.provider == "fal" and not body.admission_id:
        raise HTTPException(status_code=422, detail="FAL queue entries require a compiled admission")
    admission = get_or_404(db, models.FalAdmission, str(body.admission_id))
    if admission.workspace_id != workspace.id or admission.state != "ready":
        raise HTTPException(status_code=409, detail="compiled FAL admission is not ready for this workspace")
    project_id = str(body.context.get("project_id") or "")
    model_id = str(body.context.get("model_id") or "") or None
    project = db.get(models.Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    if project.workspace_id != workspace.id:
        raise HTTPException(status_code=409, detail="project belongs to another workspace")
    version_ids = [str(value) for value in body.model_version_ids]
    for version_id in version_ids:
        version = get_or_404(db, models.ModelVersion, version_id)
        selected_model = get_or_404(db, models.Model, version.model_id)
        if selected_model.project_id != project_id or (model_id and selected_model.id != model_id):
            raise HTTPException(status_code=409, detail="selected model version does not match queue context")
    subject = db.scalar(select(models.FalSubject).where(models.FalSubject.admission_id == admission.id).order_by(models.FalSubject.ordinal))
    binding = dict(subject.definition or {}) if subject else {}
    snapshot = get_or_404(db, models.StoragePolicySnapshot, admission.snapshot_id)
    if (not subject or binding.get("project_id") != project_id or binding.get("model_version_id") not in version_ids
        or snapshot.operation_id != str(body.compiled_request_id) or str(body.compiled_request_id) != str(body.client_request_id)):
        raise HTTPException(status_code=409, detail="compiled FAL admission does not match queued request context")
    item = models.GenerationQueueItem(workspace_id=workspace.id, client_request_id=str(body.client_request_id), workflow=body.workflow,
        provider=body.provider, queue_owner=capability["owner"], project_id=project_id, model_id=model_id,
        model_version_ids=version_ids, compiled_request_id=str(body.compiled_request_id),
        admission_id=str(body.admission_id), state="queued", progress={"completed": 0, "total": admission.expected_artifact_count},
        result={}, request_snapshot=body.request)
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(models.GenerationQueueItem).where(models.GenerationQueueItem.workspace_id == workspace.id, models.GenerationQueueItem.client_request_id == str(body.client_request_id)))
        if existing:
            return projection(existing)
        raise
    db.refresh(item)
    return projection(item)
