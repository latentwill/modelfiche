from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..integrations.fal.adapters import ValidationError, get_adapter, list_adapters, normalize_parameters
from ..integrations.fal.credentials import resolve_fal_credential
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..settings import get_settings
from .collaboration import create_eval_run, dump, enqueue_grid, grid_definition

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


@router.get("/config")
def public_config(db: DB):
    settings = get_settings()
    sources = db.scalars(select(models.ImportSource).where(models.ImportSource.is_active.is_(True)).order_by(models.ImportSource.name)).all()
    return {
        "database": "sqlite",
        "asset_root": str(settings.asset_root),
        "cache_root": str(settings.cache_root),
        "authentication": False,
        "fal_configured": resolve_fal_credential() is not None,
        "import_sources": [{"id": source.id, "name": source.name, "provider": source.provider, "bucket": source.bucket, "allowed_prefixes": source.allowed_prefixes} for source in sources],
    }


def endpoint_row(adapter) -> dict[str, Any]:
    return {
        "id": adapter.endpoint_id,
        "endpoint_id": adapter.endpoint_id,
        "endpoint_adapter": adapter.endpoint_id,
        "name": adapter.display_name,
        "defaults": adapter.defaults,
        "fields": adapter.field_schema,
        "grid_axes": sorted(adapter.allowed_axes),
        "request_fields": sorted(adapter.allowed_request_fields),
        "grid": adapter.grid_contract(),
        "compatible_base_model_markers": list(adapter.compatible_base_model_markers),
        "configured": resolve_fal_credential() is not None,
    }


@router.get("/eval-endpoints")
def eval_endpoints():
    return [endpoint_row(adapter) for adapter in list_adapters()]


@router.get("/eval-endpoints/{endpoint_id:path}/schema")
def eval_endpoint_schema(endpoint_id: str):
    try:
        return endpoint_row(get_adapter(endpoint_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/eval-runs/preview", include_in_schema=False)
def preview_eval_run(body: schemas.EvalRunCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    return create_eval_run(body.model_copy(update={"dry_run": True}), db, x_profile_id)


class GridCreate(schemas.ORMModel):
    model_version_id: str
    prompt_set_id: str
    endpoint: str | None = None
    endpoint_adapter: str | None = None
    name: str | None = None
    x_axis: dict[str, Any]
    y_axis: dict[str, Any]
    parameters: dict[str, Any] = {}


@router.post("/grids", status_code=status.HTTP_201_CREATED)
def create_grid(body: GridCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    endpoint = body.endpoint or body.endpoint_adapter
    if not endpoint:
        raise HTTPException(status_code=422, detail="endpoint or endpoint_adapter is required")
    try:
        adapter = get_adapter(endpoint)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    version = get_or_404(db, models.ModelVersion, body.model_version_id)
    model = get_or_404(db, models.Model, version.model_id)
    prompt_set = get_or_404(db, models.PromptSet, body.prompt_set_id)
    if prompt_set.project_id != model.project_id:
        raise HTTPException(status_code=400, detail="model version and prompt set belong to different projects")
    x_axis = {"parameter": body.x_axis.get("parameter") or body.x_axis.get("field"), "values": body.x_axis.get("values", [])}
    y_axis = {"parameter": body.y_axis.get("parameter") or body.y_axis.get("field"), "values": body.y_axis.get("values", [])}
    fields = {x_axis["parameter"], y_axis["parameter"]}
    if None in fields or len(fields) != 2 or not fields.issubset(adapter.allowed_axes):
        raise HTTPException(status_code=422, detail="grid axes must be different fields supported by the endpoint")
    if not x_axis["values"] or not y_axis["values"]:
        raise HTTPException(status_code=422, detail="both grid axes require values")
    try:
        for axis in (x_axis, y_axis):
            axis["values"] = [adapter.parse_grid_axis_value(str(axis["parameter"]), value) for value in axis["values"]]
        parameters = normalize_parameters({"endpoint_id": endpoint, **body.parameters}, grid_cell=True)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for axis in (x_axis, y_axis):
        if axis["parameter"] not in {"model", "checkpoint_step"}:
            continue
        for model_version_id in axis["values"]:
            axis_version = get_or_404(db, models.ModelVersion, str(model_version_id))
            axis_model = get_or_404(db, models.Model, axis_version.model_id)
            axis_endpoint = str((axis_version.readiness or {}).get("endpoint_id") or "")
            if axis_model.project_id != model.project_id or axis_endpoint != endpoint:
                raise HTTPException(status_code=422, detail="grid model axes must use project checkpoints registered to the selected endpoint")
    definition = db.scalar(select(models.EvalDefinition).where(models.EvalDefinition.project_id == model.project_id, models.EvalDefinition.endpoint == endpoint, models.EvalDefinition.model_version_id == version.id, models.EvalDefinition.prompt_set_id == prompt_set.id, models.EvalDefinition.parameters == parameters))
    if definition is None:
        definition = models.EvalDefinition(project_id=model.project_id, name=f"{model.name} / {prompt_set.name} grid", endpoint=endpoint, model_version_id=version.id, prompt_set_id=prompt_set.id, parameters=parameters)
        db.add(definition)
        db.flush()
    grid = models.GridDefinition(project_id=model.project_id, name=body.name or f"{model.name} grid", eval_definition_id=definition.id, x_axis=x_axis, y_axis=y_axis)
    db.add(grid)
    db.flush()
    record_activity(db, action="grid.created", subject_type="grid_definition", subject_id=grid.id, profile_id=active_profile(db, x_profile_id).id, project_id=grid.project_id)
    db.commit()
    return {**dump(grid), "endpoint_adapter": endpoint, "model_version_id": version.id, "prompt_set_id": prompt_set.id}


@router.get("/grids")
def grids(db: DB, project_id: str | None = None):
    query = select(models.GridDefinition).order_by(models.GridDefinition.created_at.desc())
    if project_id:
        query = query.where(models.GridDefinition.project_id == project_id)
    rows = []
    for grid in db.scalars(query):
        definition = db.get(models.EvalDefinition, grid.eval_definition_id)
        rows.append({**dump(grid), "endpoint_adapter": definition.endpoint if definition else None, "status": "saved"})
    return rows


@router.get("/grids/{grid_id}")
def grid(grid_id: str, db: DB):
    return grid_definition(grid_id, db)


@router.post("/grids/{grid_id}/run", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
@router.post("/grids/{grid_id}/runs", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
def run_grid(grid_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    return enqueue_grid(grid_id, db, x_profile_id)


@router.get("/grid-runs/{grid_id}/cells")
def grid_cells(grid_id: str, db: DB):
    return grid_definition(grid_id, db)["cells"]


@router.get("/import-jobs/{import_job_id}")
def import_job(import_job_id: str, db: DB):
    item = get_or_404(db, models.ImportJob, import_job_id)
    job = db.get(models.Job, item.job_id) if item.job_id else None
    row = dump(item)
    if job:
        job_state = job.state.value if hasattr(job.state, "value") else str(job.state)
        row.update(
            state="cancel_requested" if job.cancel_requested and job_state not in {"succeeded", "failed", "canceled"} else job_state,
            progress=job.progress,
            error=job.error,
        )
    return {**row, "job": dump(job) if job else None}


@router.post("/import-jobs/{import_job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_import(import_job_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    item = get_or_404(db, models.ImportJob, import_job_id)
    prior = db.get(models.Job, item.job_id) if item.job_id else None
    payload = dict(prior.payload) if prior else {"source_id": item.source_id, "project_id": item.project_id, "prefix": item.prefix}
    job = models.Job(workspace_id=current_workspace(db).id, kind="s3.import", profile_id=active_profile(db, x_profile_id).id, payload=payload)
    db.add(job)
    db.flush()
    item.job_id, item.state, item.progress = job.id, "queued", 0
    db.commit()
    return {"id": item.id, "job_id": job.id, "state": "queued"}


@router.post("/import-jobs/{import_job_id}/cancel")
def cancel_import(import_job_id: str, db: DB):
    item = get_or_404(db, models.ImportJob, import_job_id)
    job = db.get(models.Job, item.job_id) if item.job_id else None
    if job and job.state not in {models.JobState.succeeded, models.JobState.failed, models.JobState.canceled}:
        job.cancel_requested = True
    item.state = "cancel_requested"
    db.commit()
    return {**dump(item), "job_id": job.id if job else None}
