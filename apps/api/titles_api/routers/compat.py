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
from .collaboration import dump, grid_definition

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








@router.get("/grids")
def grids(db: DB, project_id: str | None = None):
    query = select(models.GridDefinition).order_by(models.GridDefinition.created_at.desc())
    if project_id:
        query = query.where(models.GridDefinition.project_id == project_id)
    grid_rows = db.scalars(query).all()
    definition_ids = {grid.eval_definition_id for grid in grid_rows if grid.eval_definition_id}
    definitions = {
        definition.id: definition
        for definition in db.scalars(select(models.EvalDefinition).where(models.EvalDefinition.id.in_(definition_ids))).all()
    } if definition_ids else {}
    return [
        {**dump(grid), "endpoint_adapter": definitions[grid.eval_definition_id].endpoint if grid.eval_definition_id in definitions else None, "status": "saved"}
        for grid in grid_rows
    ]


@router.get("/grids/{grid_id}")
def grid(grid_id: str, db: DB):
    return grid_definition(grid_id, db)




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
