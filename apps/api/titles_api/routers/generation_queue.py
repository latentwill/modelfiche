from collections import defaultdict
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
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
    children: list["QueueChildCreate"] = Field(default_factory=list)

class QueueChildCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_version_id: UUID
    compiled_request_id: UUID
    admission_id: UUID

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
def projection(
    item: models.GenerationQueueItem,
    children: list[models.GenerationQueueChild] | None = None,
    grid_definition_id: str | None = None,
) -> dict[str, Any]:
    if children is None:
        session = Session.object_session(item)
        if session:
            children = list(session.scalars(select(models.GenerationQueueChild).where(
                models.GenerationQueueChild.queue_item_id == item.id
            ).order_by(models.GenerationQueueChild.ordinal)))
    children = children or []
    grid_definition_id = grid_definition_id or (item.request_snapshot or {}).get("grid_definition_id")
    if item.workflow == "grid" and not grid_definition_id:
        grid_cell_id = next((str(child.result.get("grid_cell_id")) for child in children
            if isinstance(child.result, dict) and child.result.get("grid_cell_id")), None)
        if grid_cell_id:
            session = Session.object_session(item)
            cell = session.get(models.GridCell, grid_cell_id) if session else None
            grid_definition_id = cell.grid_definition_id if cell else None
    return {"id": item.id, "workflow": item.workflow, "provider": item.provider, "queue_owner": item.queue_owner,
        "project_id": item.project_id, "model_id": item.model_id, "model_version_ids": item.model_version_ids,
        "grid_definition_id": grid_definition_id,
        "compiled_request_id": item.compiled_request_id, "admission_id": item.admission_id, "client_request_id": item.client_request_id,
        "status": item.state, "progress": item.progress, "result": item.result, "error": item.error,
        "provider_job_id": item.provider_job_id, "job_id": item.job_id, "request": item.request_snapshot,
        "children": [{"id": child.id, "ordinal": child.ordinal, "model_version_id": child.model_version_id,
            "compiled_request_id": child.compiled_request_id, "admission_id": child.admission_id, "job_id": child.job_id,
            "provider_job_id": child.provider_job_id, "status": child.state, "progress": child.progress,
            "result": child.result, "error": child.error} for child in children],
        "created_at": item.created_at, "updated_at": item.updated_at}


def batch_projection(db: Session, items: list[models.GenerationQueueItem]) -> list[dict[str, Any]]:
    if not items:
        return []
    item_ids = [item.id for item in items]
    children_by_item: dict[str, list[models.GenerationQueueChild]] = defaultdict(list)
    children = db.scalars(select(models.GenerationQueueChild).where(
        models.GenerationQueueChild.queue_item_id.in_(item_ids)
    ).order_by(models.GenerationQueueChild.queue_item_id, models.GenerationQueueChild.ordinal)).all()
    cell_ids = {
        str(child.result.get("grid_cell_id"))
        for child in children
        if isinstance(child.result, dict) and child.result.get("grid_cell_id")
    }
    cells = {
        cell.id: cell.grid_definition_id
        for cell in db.scalars(select(models.GridCell).where(models.GridCell.id.in_(cell_ids))).all()
    } if cell_ids else {}
    for child in children:
        children_by_item[child.queue_item_id].append(child)
    return [
        projection(item, children_by_item.get(item.id), next((
            cells[str(child.result.get("grid_cell_id"))]
            for child in children_by_item.get(item.id, [])
            if isinstance(child.result, dict) and child.result.get("grid_cell_id")
            and str(child.result.get("grid_cell_id")) in cells
        ), None))
        for item in items
    ]

def owned_queue_item(db: Session, item_id: str) -> models.GenerationQueueItem:
    workspace = current_workspace(db)
    item = db.scalar(select(models.GenerationQueueItem).where(models.GenerationQueueItem.id == item_id,
        models.GenerationQueueItem.workspace_id == workspace.id))
    if not item:
        raise HTTPException(status_code=404, detail="generation queue item not found")
    return item

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
    return {"items": batch_projection(db, items)}

@router.get("/generation-queue/{item_id}")
def get_queue_item(item_id: str, db: DB):
    return projection(owned_queue_item(db, item_id))

@router.get("/generation-queue/{item_id}/eval-results")
def eval_queue_results(
    item_id: str,
    db: DB,
    full: bool = Query(False, description="Return all outputs instead of the bounded latest output window."),
    output_limit: int = Query(100, ge=1, le=1000),
):
    item = owned_queue_item(db, item_id)
    if item.workflow != "eval":
        raise HTTPException(status_code=409, detail="queue item is not an Eval workflow")
    children = list(db.scalars(select(models.GenerationQueueChild).where(
        models.GenerationQueueChild.queue_item_id == item.id
    ).order_by(models.GenerationQueueChild.ordinal)))
    admission_ids = {child.admission_id for child in children if child.admission_id}
    admissions = {
        admission.id: admission
        for admission in db.scalars(select(models.FalAdmission).where(models.FalAdmission.id.in_(admission_ids))).all()
    } if admission_ids else {}
    run_ids = {admission.eval_run_id for admission in admissions.values() if admission.eval_run_id}
    runs_by_id = {
        run.id: run
        for run in db.scalars(select(models.EvalRun).where(models.EvalRun.id.in_(run_ids))).all()
    } if run_ids else {}
    version_ids = {child.model_version_id for child in children if child.model_version_id}
    versions = {
        version.id: version
        for version in db.scalars(select(models.ModelVersion).where(models.ModelVersion.id.in_(version_ids))).all()
    } if version_ids else {}
    model_ids = {version.model_id for version in versions.values() if version.model_id}
    models_by_id = {
        model.id: model
        for model in db.scalars(select(models.Model).where(models.Model.id.in_(model_ids))).all()
    } if model_ids else {}
    definition_ids = {run.definition_id for run in runs_by_id.values() if run.definition_id}
    definitions = {
        definition.id: definition
        for definition in db.scalars(select(models.EvalDefinition).where(models.EvalDefinition.id.in_(definition_ids))).all()
    } if definition_ids else {}
    checkpoint_ids = {version.checkpoint_id for version in versions.values() if version.checkpoint_id}
    checkpoints = {
        checkpoint.id: checkpoint
        for checkpoint in db.scalars(select(models.Checkpoint).where(models.Checkpoint.id.in_(checkpoint_ids))).all()
    } if checkpoint_ids else {}
    outputs_by_run: dict[str, list[models.EvalOutput]] = defaultdict(list)
    if run_ids:
        output_query = select(
            models.EvalOutput,
            func.row_number().over(
                partition_by=models.EvalOutput.eval_run_id,
                order_by=(models.EvalOutput.created_at.desc(), models.EvalOutput.id.desc()),
            ).label("output_rank"),
        ).where(models.EvalOutput.eval_run_id.in_(run_ids))
        for output, rank in db.execute(output_query):
            if full or rank <= output_limit:
                outputs_by_run[output.eval_run_id].append(output)
        for output_rows in outputs_by_run.values():
            output_rows.sort(key=lambda output: (output.created_at, output.id))
    projected_runs = []
    for child in children:
        admission = admissions.get(child.admission_id)
        run = runs_by_id.get(admission.eval_run_id) if admission and admission.eval_run_id else None
        version = versions.get(child.model_version_id)
        model = models_by_id.get(version.model_id) if version else None
        definition = definitions.get(run.definition_id) if run else None
        outputs = []
        for output in outputs_by_run.get(run.id, []) if run and definition else []:
            inline_id = str((output.provider_metadata or {}).get("inline_prompt_id") or output.prompt_id or "")
            inline = next((entry for entry in (definition.inline_prompts or []) if str(entry.get("id")) == inline_id), {})
            outputs.append({"id": output.id, "asset_id": output.asset_id, "asset_revision_id": output.asset_id,
                "eval_run_id": run.id, "prompt_id": inline_id, "prompt": inline.get("text"), "seed": output.seed,
                "generated_at": output.generated_at, "provider_metadata": output.provider_metadata})
        checkpoint = checkpoints.get(version.checkpoint_id) if version else None
        projected_runs.append({"ordinal": child.ordinal, "status": child.state, "eval_run_id": run.id if run else None,
            "model_id": model.id if model else None, "model_name": model.name if model else None,
            "model_version_id": child.model_version_id, "model_version_name": version.name if version else None,
            "checkpoint_step": checkpoint.step if checkpoint else None,
            "prompts": definition.inline_prompts if definition else [], "outputs": outputs})
    return {**projection(item, children), "runs": projected_runs}

@router.get("/generation-queue/{item_id}/grid-results")
def grid_queue_results(item_id: str, db: DB):
    """Project ingested child outputs back onto stable snapshot indexes."""
    item = owned_queue_item(db, item_id)
    if item.workflow != "grid":
        raise HTTPException(status_code=409, detail="queue item is not a Grid workflow")
    snapshot_cells = list((item.request_snapshot or {}).get("cells") or [])
    children = list(db.scalars(select(models.GenerationQueueChild).where(
        models.GenerationQueueChild.queue_item_id == item.id
    ).order_by(models.GenerationQueueChild.ordinal)))
    admission_ids = {child.admission_id for child in children if child.admission_id}
    admissions = {
        admission.id: admission
        for admission in db.scalars(select(models.FalAdmission).where(models.FalAdmission.id.in_(admission_ids))).all()
    } if admission_ids else {}
    run_ids = {admission.eval_run_id for admission in admissions.values() if admission.eval_run_id}
    runs_by_id = {
        run.id: run
        for run in db.scalars(select(models.EvalRun).where(models.EvalRun.id.in_(run_ids))).all()
    } if run_ids else {}
    outputs_by_run: dict[str, models.EvalOutput] = {}
    if run_ids:
        ranked_outputs = select(
            models.EvalOutput,
            func.row_number().over(
                partition_by=models.EvalOutput.eval_run_id,
                order_by=(models.EvalOutput.created_at, models.EvalOutput.id),
            ).label("output_rank"),
        ).where(models.EvalOutput.eval_run_id.in_(run_ids)).subquery()
        outputs_by_run = {
            row.eval_run_id: row
            for row in db.scalars(select(models.EvalOutput).join(
                ranked_outputs, models.EvalOutput.id == ranked_outputs.c.id
            ).where(ranked_outputs.c.output_rank == 1)).all()
        }
    cells = []
    for child in children:
        cell = dict(snapshot_cells[child.ordinal]) if child.ordinal < len(snapshot_cells) else {"ordinal": child.ordinal}
        admission = admissions.get(child.admission_id)
        run = runs_by_id.get(admission.eval_run_id) if admission and admission.eval_run_id else None
        output = outputs_by_run.get(run.id) if run else None
        cells.append({**cell, "queue_child_id": child.id, "status": child.state, "error": child.error,
            "eval_run_id": run.id if run else None, "asset_id": output.asset_id if output else None,
            "asset_revision_id": output.asset_id if output else None, "generated_at": output.generated_at if output else None})
    return {**projection(item, children), "cells": cells}

def _claim_item(db: Session, item: models.GenerationQueueItem) -> dict[str, Any] | None:
    claimed = db.execute(update(models.GenerationQueueItem).where(
        models.GenerationQueueItem.id == item.id,
        models.GenerationQueueItem.state == "queued",
        models.GenerationQueueItem.queue_owner == "dam",
    ).values(state="running"))
    if claimed.rowcount != 1:
        db.rollback()
        return None
    child = db.scalar(select(models.GenerationQueueChild).where(
        models.GenerationQueueChild.queue_item_id == item.id,
        models.GenerationQueueChild.state == "queued",
    ).order_by(models.GenerationQueueChild.ordinal).limit(1))
    # Legacy rows created before ordered children remain runnable.
    admission_id = child.admission_id if child else item.admission_id
    # Full binding validation prevents a compiled admission being attached to another context/model.
    admission = get_or_404(db, models.FalAdmission, admission_id)
    subject = db.scalar(select(models.FalSubject).where(
        models.FalSubject.admission_id == admission.id,
    ).order_by(models.FalSubject.ordinal))
    definition = dict(subject.definition or {}) if subject else {}
    if (admission.workspace_id != item.workspace_id or definition.get("project_id") != item.project_id
        or definition.get("model_version_id") not in item.model_version_ids
        or (child and definition.get("model_version_id") != child.model_version_id)
        or str(definition.get("admission_input_digest") or "") == ""):
        item.state = "failed"
        item.error = "compiled admission does not match queued context/models"
        if child:
            child.state = "failed"
            child.error = item.error
        db.commit()
        return projection(item)
    db.flush()
    accepted = admit_fal_admission(admission.id, FalAdmitRequestV1(
        expected_version=admission.version,
        billing_acknowledgement=BILLING_ACKNOWLEDGEMENT,
    ), db)
    job = db.scalar(select(models.Job).where(
        models.Job.payload["admission_id"].as_string() == admission.id,
    ).order_by(models.Job.created_at.desc()))
    db.refresh(item)
    item.job_id = job.id if job else None
    item.provider_job_id = admission.eval_run_id
    if child:
        child.job_id = job.id if job else None
        child.provider_job_id = admission.eval_run_id
        child.state = "running"
    db.commit()
    db.refresh(item)
    return projection(item)


def claim_next_for_worker(db: Session) -> dict[str, Any] | None:
    """Claim the oldest DAM-owned item across all workspaces."""
    item = db.scalar(select(models.GenerationQueueItem).where(
        models.GenerationQueueItem.queue_owner == "dam",
        models.GenerationQueueItem.state == "queued",
    ).order_by(models.GenerationQueueItem.created_at.asc(), models.GenerationQueueItem.id.asc()).limit(1))
    return _claim_item(db, item) if item else None


@router.post("/generation-queue/claim")
def claim_next(db: DB):
    """Atomically claim one DAM-owned FIFO item in the request workspace."""
    workspace = current_workspace(db)
    item = db.scalar(select(models.GenerationQueueItem).where(
        models.GenerationQueueItem.workspace_id == workspace.id,
        models.GenerationQueueItem.queue_owner == "dam",
        models.GenerationQueueItem.state == "queued",
    ).order_by(models.GenerationQueueItem.created_at.asc(), models.GenerationQueueItem.id.asc()).limit(1))
    return _claim_item(db, item) if item else None

@router.patch("/generation-queue/{item_id}")
def update_queue_item(item_id: str, body: QueueStateUpdate, db: DB):
    item = owned_queue_item(db, item_id)
    item.state = normalize_state(body.status)
    if body.progress is not None: item.progress = body.progress
    if body.result is not None: item.result = body.result
    item.error = body.error
    if body.provider_job_id is not None: item.provider_job_id = body.provider_job_id
    db.commit(); db.refresh(item)
    return projection(item)

@router.post("/generation-queue/{item_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_queue_item(item_id: str, db: DB):
    item = owned_queue_item(db, item_id)
    if item.state != "failed":
        raise HTTPException(status_code=409, detail="only failed generation work can be retried")
    children = list(db.scalars(select(models.GenerationQueueChild).where(
        models.GenerationQueueChild.queue_item_id == item.id,
    ).order_by(models.GenerationQueueChild.ordinal)))
    failed_children = [child for child in children if child.state == "failed"]
    retry_targets = failed_children or ([None] if not children else [])
    if not retry_targets:
        raise HTTPException(status_code=409, detail="no failed generation attempt is available to retry")
    replacement_jobs = []
    for child in retry_targets:
        prior_job_id = child.job_id if child else item.job_id
        prior = db.get(models.Job, prior_job_id) if prior_job_id else None
        if prior is None:
            raise HTTPException(status_code=409, detail="the failed attempt has no recoverable job payload")
        replacement = models.Job(
            workspace_id=prior.workspace_id,
            kind=prior.kind,
            state=models.JobState.queued,
            profile_id=prior.profile_id,
            payload=dict(prior.payload or {}),
            progress=0,
            result={},
        )
        db.add(replacement)
        db.flush()
        replacement_jobs.append(replacement.id)
        if child:
            child.job_id = replacement.id
            child.state = "queued"
            child.error = None
            child.progress = {**dict(child.progress or {}), "fraction": 0}
        else:
            item.job_id = replacement.id
    if children:
        item.job_id = replacement_jobs[0]
    item.state = "queued"
    item.error = None
    item.provider_job_id = None
    db.commit()
    db.refresh(item)
    return {**projection(item), "retry_job_ids": replacement_jobs}


@router.post("/generation-queue", status_code=status.HTTP_202_ACCEPTED)
def enqueue(body: QueueCreate, db: DB):
    workspace = current_workspace(db)
    existing = db.scalar(select(models.GenerationQueueItem).where(models.GenerationQueueItem.workspace_id == workspace.id, models.GenerationQueueItem.client_request_id == str(body.client_request_id)))
    if existing:
        if existing.compiled_request_id != str(body.compiled_request_id) or existing.request_snapshot != body.request:
            raise HTTPException(status_code=409, detail="client_request_id already identifies a different queued request")
        if body.children:
            stored = list(db.scalars(select(models.GenerationQueueChild).where(models.GenerationQueueChild.queue_item_id == existing.id).order_by(models.GenerationQueueChild.ordinal)))
            requested = [(str(child.model_version_id), str(child.compiled_request_id), str(child.admission_id)) for child in body.children]
            if [(child.model_version_id, child.compiled_request_id, child.admission_id) for child in stored] != requested:
                raise HTTPException(status_code=409, detail="client_request_id already identifies different queued children")
        return projection(existing)
    capability = queue_capability(body.provider)
    if capability["dispatch"] == "provider_native":
        raise HTTPException(status_code=422, detail=f"{body.provider} owns its provider-native queue; DAM must reference an external job instead of scheduling it")
    if capability["dispatch"] != "internal":
        raise HTTPException(status_code=422, detail=f"generation provider is not installed: {body.provider}")
    if body.provider == "fal" and not body.admission_id and not body.children:
        raise HTTPException(status_code=422, detail="FAL queue entries require a compiled admission")
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
    child_specs = body.children or [QueueChildCreate(model_version_id=body.model_version_ids[0], compiled_request_id=body.compiled_request_id, admission_id=body.admission_id)]
    if any(str(child.model_version_id) not in version_ids for child in child_specs):
        raise HTTPException(status_code=409, detail="ordered queue children include a model outside the selected models")
    if set(version_ids) != {str(child.model_version_id) for child in child_specs}:
        raise HTTPException(status_code=409, detail="each selected model must have at least one ordered queue child")
    admissions = []
    for child in child_specs:
        admission = get_or_404(db, models.FalAdmission, str(child.admission_id))
        if admission.workspace_id != workspace.id or admission.state != "ready":
            raise HTTPException(status_code=409, detail="compiled FAL admission is not ready for this workspace")
        subject = db.scalar(select(models.FalSubject).where(models.FalSubject.admission_id == admission.id).order_by(models.FalSubject.ordinal))
        binding = dict(subject.definition or {}) if subject else {}
        snapshot = get_or_404(db, models.StoragePolicySnapshot, admission.snapshot_id)
        if (not subject or binding.get("project_id") != project_id or binding.get("model_version_id") != str(child.model_version_id)
            or snapshot.operation_id != str(child.compiled_request_id)):
            raise HTTPException(status_code=409, detail="compiled FAL admission does not match queued request context")
        admissions.append(admission)
    item = models.GenerationQueueItem(workspace_id=workspace.id, client_request_id=str(body.client_request_id), workflow=body.workflow,
        provider=body.provider, queue_owner=capability["owner"], project_id=project_id, model_id=model_id,
        model_version_ids=version_ids, compiled_request_id=str(body.compiled_request_id),
        admission_id=str(child_specs[0].admission_id), state="queued", progress={"completed": 0, "total": sum(row.expected_artifact_count for row in admissions)},
        result={}, request_snapshot=body.request)
    db.add(item)
    db.flush()
    for ordinal, child in enumerate(child_specs):
        db.add(models.GenerationQueueChild(queue_item_id=item.id, ordinal=ordinal, model_version_id=str(child.model_version_id),
            compiled_request_id=str(child.compiled_request_id), admission_id=str(child.admission_id), state="queued",
            progress={"completed": 0, "total": admissions[ordinal].expected_artifact_count}, result={}))
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
