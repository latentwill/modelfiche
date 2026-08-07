from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..experiment_plans import CONTRACT_VERSION, resolve_plan
from ..integrations.fal.adapters import get_adapter
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..storage.fal_admissions import FalAdmissionValidationError, FalSubjectDraft, build_admission_plan

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def _dump(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.key) for column in row.__table__.columns}


def _project(db: Session, project_id: str) -> models.Project:
    project = get_or_404(db, models.Project, project_id)
    if project.workspace_id != current_workspace(db).id:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _hydrate_input(db: Session, project_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    reserved = {"loras", "registered_lora", "_server_hydrated", "provider_url", "fal_url", "fal_path", "url", "path"}

    def reject(value: Any, location: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in reserved:
                    raise HTTPException(status_code=422, detail=f"{location}.{key} is server-owned")
                reject(nested, f"{location}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                reject(nested, f"{location}[{index}]")

    reject(raw.get("shared_params", {}), "shared_params")
    for axis_name, axis in (raw.get("axes") or {}).items():
        if axis is None:
            continue
        if not isinstance(axis, dict):
            raise HTTPException(status_code=422, detail=f"axes.{axis_name} must be an object")
        if str(axis.get("name", "")).lower() in reserved:
            raise HTTPException(status_code=422, detail=f"axes.{axis_name}.name is server-owned")
        reject(axis, f"axes.{axis_name}")
    for target in raw.get("targets", []):
        if not isinstance(target, dict):
            raise HTTPException(status_code=422, detail="target must be an object")
        reject(target, "targets")
    hydrated = deepcopy(raw)
    for target in hydrated.get("targets", []):
        overrides = target.get("overrides", target.get("target_overrides", {}))
        if not isinstance(overrides, dict):
            raise HTTPException(status_code=422, detail="target overrides must be an object")
        if "loras" in overrides:
            raise HTTPException(status_code=422, detail="target LoRA path is server-owned")
        version_id = target.get("model_version_id")
        if not version_id:
            continue
        version = get_or_404(db, models.ModelVersion, str(version_id))
        model = get_or_404(db, models.Model, version.model_id)
        target_project = get_or_404(db, models.Project, model.project_id)
        grid_project = get_or_404(db, models.Project, project_id)
        if target_project.workspace_id != grid_project.workspace_id:
            raise HTTPException(status_code=404, detail="target model version was not found")
        checkpoint = get_or_404(db, models.Checkpoint, version.checkpoint_id)
        canonical_revision_id = version.checkpoint_revision_id or checkpoint.current_revision_id
        if not canonical_revision_id:
            raise HTTPException(status_code=409, detail="selected model version has no immutable checkpoint revision")
        supplied_revision_id = target.get("checkpoint_revision_id")
        if supplied_revision_id and str(supplied_revision_id) != str(canonical_revision_id):
            raise HTTPException(status_code=409, detail="checkpoint revision does not match model version")
        readiness = dict(version.readiness or {})
        fal_url = str(readiness.get("fal_url") or readiness.get("fal_path") or "")
        registered_endpoint = str(readiness.get("endpoint_id") or readiness.get("endpoint") or readiness.get("fal_endpoint") or "")
        if not fal_url or not registered_endpoint:
            raise HTTPException(status_code=409, detail="selected model version is not registered with FAL")
        endpoint_id = str(target.get("endpoint_id") or registered_endpoint)
        try:
            adapter = get_adapter(endpoint_id)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=f"requested FAL endpoint is unavailable: {endpoint_id}") from exc
        if endpoint_id != registered_endpoint and not adapter.supports_base_model(model.base_model):
            raise HTTPException(status_code=409, detail=f"requested endpoint is incompatible with base model {model.base_model}")
        scale = overrides.get("lora_scale", 1.0)
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0 <= float(scale) <= 4:
            raise HTTPException(status_code=422, detail="target lora_scale must be between 0 and 4")
        target["checkpoint_revision_id"] = str(canonical_revision_id)
        target["_server_hydrated"] = True
        target["registered_lora"] = {"path": fal_url, "scale": float(scale)}
    return hydrated


def _resolve_body(body: schemas.PlanPayload, project_id: str, db: Session) -> dict[str, Any]:
    try:
        raw = _hydrate_input(db, project_id, body.model_dump())
        return resolve_plan(raw, project_id)
    except HTTPException:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _preflight(body: schemas.PlanPayload, project_id: str, db: Session) -> dict[str, Any]:
    resolved = _resolve_body(body, project_id, db)
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": project_id,
        "plan_version": resolved["plan"]["plan_version"],
        "digest": resolved["plan"]["digest"],
        "valid": resolved["valid"],
        "schema_errors": resolved["schema_errors"],
        "cells": resolved["cells"],
        "request_count": resolved["request_count"],
        "estimated_cost": resolved["estimated_cost"],
    }
def _attachment_job_key(project_id: str, grid_id: str, cell_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"grid-attach:{project_id}:{grid_id}:{cell_id}:{digest}"


def _attachment_workflow(
    db: Session,
    project_id: str,
    output_asset_id: str,
    body: schemas.GridCellAttachRequest,
) -> dict[str, Any] | None:
    if body.workflow is None:
        return None
    reference = body.workflow.model_dump(mode="json", exclude_none=True)
    workflow_asset_id = reference.get("asset_id")
    if workflow_asset_id:
        workflow_asset = db.scalar(
            select(models.Asset).where(
                models.Asset.id == workflow_asset_id,
                models.Asset.workspace_id == current_workspace(db).id,
            )
        )
        if workflow_asset is None or workflow_asset.project_id not in {None, project_id}:
            raise HTTPException(status_code=404, detail="workflow asset not found")
        if workflow_asset.id == output_asset_id:
            raise HTTPException(status_code=422, detail="workflow asset must differ from output asset")
        reference["asset_name"] = workflow_asset.name
    return reference


def _attachment_marker(grid: models.GridDefinition, provider: str) -> dict[str, str]:
    return {
        "source": "existing_asset_attachment",
        "grid_definition_id": str(grid.id),
        "provider": provider,
    }


def _attachment_run(
    db: Session,
    project: models.Project,
    grid: models.GridDefinition,
    provider: str,
    workflow: dict[str, Any] | None,
) -> models.EvalRun:
    marker = _attachment_marker(grid, provider)
    definition = db.get(models.EvalDefinition, grid.eval_definition_id) if grid.eval_definition_id else None
    if definition is not None and dict(definition.parameters or {}).get("_grid_attachment") != marker:
        definition = None
    if definition is None:
        definitions = db.scalars(
            select(models.EvalDefinition)
            .where(
                models.EvalDefinition.project_id == project.id,
                models.EvalDefinition.endpoint == provider,
            )
        ).all()
        definition = next(
            (
                candidate
                for candidate in definitions
                if dict(candidate.parameters or {}).get("_grid_attachment") == marker
            ),
            None,
        )
    if definition is None:
        definition = models.EvalDefinition(
            project_id=project.id,
            name=f"{grid.name} · {provider} imported outputs",
            endpoint=provider,
            inline_prompts=list((grid.plan_snapshot or {}).get("cases") or []),
            parameters={
                "_grid_attachment": marker,
                "provider": provider,
                "workflow": workflow or {},
            },
            plan_id=grid.plan_id,
            plan_version=grid.plan_version,
            plan_digest=grid.plan_digest,
        )
        db.add(definition)
        db.flush()
        grid.eval_definition_id = definition.id
    runs = db.scalars(
        select(models.EvalRun)
        .where(
            models.EvalRun.definition_id == definition.id,
            models.EvalRun.status == "succeeded",
        )
        .order_by(models.EvalRun.created_at.desc())
    ).all()
    run = next(
        (
            candidate
            for candidate in runs
            if dict(candidate.parameters_snapshot or {}).get("_grid_attachment") == marker
        ),
        None,
    )
    if run is None:
        run = models.EvalRun(
            definition_id=definition.id,
            plan_id=grid.plan_id,
            plan_version=grid.plan_version,
            plan_digest=grid.plan_digest,
            status="succeeded",
            parameters_snapshot={
                "_grid_attachment": marker,
                "provider": provider,
                "workflow": workflow or {},
            },
        )
        db.add(run)
        db.flush()
    return run


def _attachment_provider_metadata(
    grid: models.GridDefinition,
    cell: models.GridCell,
    body: schemas.GridCellAttachRequest,
    provider: str,
    workflow: dict[str, Any] | None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {**(existing or {}), **dict(body.metadata or {})}
    grid_metadata = dict(metadata.get("grid_metadata") or {})
    grid_metadata.update(
        {
            "grid_definition_id": grid.id,
            "grid_cell_id": cell.id,
            "ordinal": cell.ordinal,
            "coordinate": dict(cell.coordinate or {}),
        }
    )
    model_version_id = (cell.target_snapshot or {}).get("model_version_id")
    if model_version_id:
        grid_metadata["model_version_id"] = model_version_id
    metadata.update(
        {
            "provider": provider,
            "generated_by": provider,
            "source": "existing_asset",
            "grid_metadata": grid_metadata,
            "attachment": {
                "kind": "existing_asset",
                "idempotency_key": body.idempotency_key,
            },
        }
    )
    if workflow is not None:
        metadata["workflow"] = workflow
    return metadata


def _attachment_response(
    grid: models.GridDefinition,
    cell: models.GridCell,
    output: models.EvalOutput,
    asset: models.Asset,
) -> dict[str, Any]:
    provider_metadata = dict(output.provider_metadata or {})
    return {
        "grid_id": grid.id,
        "grid_status": grid.status,
        "cell_id": cell.id,
        "ordinal": cell.ordinal,
        "status": cell.status,
        "eval_output_id": output.id,
        "asset_id": asset.id,
        "asset_revision_id": asset.id,
        "asset_name": asset.name,
        "provider": provider_metadata.get("provider"),
        "workflow": provider_metadata.get("workflow"),
        "generated_at": output.generated_at,
        "provider_metadata": provider_metadata,
    }

def _grid_response(db: Session, grid: models.GridDefinition) -> dict[str, Any]:
    cells = db.scalars(select(models.GridCell).where(models.GridCell.grid_definition_id == grid.id).order_by(models.GridCell.ordinal)).all()
    output_ids = [cell.eval_output_id for cell in cells if cell.eval_output_id]
    outputs = {
        output.id: output
        for output in db.scalars(select(models.EvalOutput).where(models.EvalOutput.id.in_(output_ids))).all()
    } if output_ids else {}
    version_ids = {
        str((cell.target_snapshot or {}).get("model_version_id"))
        for cell in cells
        if (cell.target_snapshot or {}).get("model_version_id")
    }
    versions = {
        version.id: version
        for version in db.scalars(select(models.ModelVersion).where(models.ModelVersion.id.in_(version_ids))).all()
    } if version_ids else {}
    checkpoint_ids = {version.checkpoint_id for version in versions.values()}
    checkpoints = {
        checkpoint.id: checkpoint
        for checkpoint in db.scalars(select(models.Checkpoint).where(models.Checkpoint.id.in_(checkpoint_ids))).all()
    } if checkpoint_ids else {}
    checkpoint_run_ids = {checkpoint.run_id for checkpoint in checkpoints.values()}
    checkpoint_runs = {
        run.id: run
        for run in db.scalars(select(models.TrainingRun).where(models.TrainingRun.id.in_(checkpoint_run_ids))).all()
    } if checkpoint_run_ids else {}
    cell_rows = []
    for cell in cells:
        output = outputs.get(cell.eval_output_id)
        version_id = str((cell.target_snapshot or {}).get("model_version_id") or "")
        version = versions.get(version_id)
        checkpoint = checkpoints.get(version.checkpoint_id) if version else None
        checkpoint_run = checkpoint_runs.get(checkpoint.run_id) if checkpoint else None
        cell_rows.append({
            **_dump(cell),
            "asset_id": output.asset_id if output else None,
            "asset_revision_id": output.asset_id if output else None,
            "seed": output.seed if output else None,
            "provider": (output.provider_metadata or {}).get("provider") if output else None,
            "workflow": (output.provider_metadata or {}).get("workflow") if output else None,
            "checkpoint_step": checkpoint.step if checkpoint else None,
            "model_version_name": version.name if version else None,
            "model_version_source_kind": checkpoint_run.run_kind if checkpoint_run else None,
        })
    jobs = db.scalars(select(models.Job).where(models.Job.payload["grid_definition_id"].as_string() == grid.id).order_by(models.Job.created_at.desc())).all()
    runs = []
    for job in jobs:
        payload = dict(job.payload or {})
        if payload.get("eval_run_id"):
            runs.append({"run_id": payload["eval_run_id"], "job_id": job.id, "status": job.state.value if hasattr(job.state, "value") else job.state})
    return {
        **_dump(grid),
        "plan": dict(grid.plan_snapshot or {}),
        "plan_version": grid.plan_version,
        "plan_digest": grid.plan_digest,
        "cells": cell_rows,
        "runs": runs,
        "request_count": grid.request_count,
        "estimated_cost": grid.estimated_cost,
    }


@router.post("/projects/{project_id}/grids/{grid_id}/cells/{cell_id}/attach", status_code=status.HTTP_201_CREATED)
def attach_existing_asset(
    project_id: str,
    grid_id: str,
    cell_id: str,
    body: schemas.GridCellAttachRequest,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    project = _project(db, project_id)
    grid = get_or_404(db, models.GridDefinition, grid_id)
    if grid.project_id != project.id:
        raise HTTPException(status_code=404, detail="grid not found")
    cell = get_or_404(db, models.GridCell, cell_id)
    if cell.grid_definition_id != grid.id:
        raise HTTPException(status_code=404, detail="grid cell not found")

    request_snapshot = body.model_dump(mode="json", exclude_none=True)
    job_key = _attachment_job_key(project.id, grid.id, cell.id, body.idempotency_key)
    existing_job = db.scalar(select(models.Job).where(models.Job.idempotency_key == job_key))
    if existing_job is not None:
        if dict(existing_job.payload or {}).get("request") != request_snapshot:
            raise HTTPException(status_code=409, detail="attachment idempotency key was already used for another request")
        return dict(existing_job.result or {})

    asset = db.scalar(
        select(models.Asset).where(
            models.Asset.id == body.asset_id,
            models.Asset.workspace_id == current_workspace(db).id,
        )
    )
    if asset is None or asset.project_id != project.id:
        raise HTTPException(status_code=404, detail="asset not found in project")
    if asset.kind != models.AssetKind.image:
        raise HTTPException(status_code=422, detail="grid cells can only attach image assets")

    provider = body.provider.strip().lower()
    if not provider:
        raise HTTPException(status_code=422, detail="provider cannot be blank")
    workflow = _attachment_workflow(db, project.id, asset.id, body)
    existing_output = db.get(models.EvalOutput, cell.eval_output_id) if cell.eval_output_id else None
    if existing_output is not None and existing_output.asset_id != asset.id:
        raise HTTPException(status_code=409, detail="grid cell already has another asset attached")
    if existing_output is None:
        run = _attachment_run(db, project, grid, provider, workflow)
        output = models.EvalOutput(
            eval_run_id=run.id,
            asset_id=asset.id,
            seed=body.seed,
            provider_metadata={},
            generated_at=body.generated_at or models.utcnow(),
        )
        db.add(output)
        db.flush()
    else:
        output = existing_output
        if body.seed is not None:
            output.seed = body.seed
        if body.generated_at is not None:
            output.generated_at = body.generated_at
        if output.generated_at is None:
            output.generated_at = models.utcnow()

    output.provider_metadata = _attachment_provider_metadata(
        grid,
        cell,
        body,
        provider,
        workflow,
        existing=dict(output.provider_metadata or {}),
    )
    effective_workflow = workflow if workflow is not None else dict(output.provider_metadata or {}).get("workflow")
    asset_metadata = {**dict(asset.metadata_ or {}), **dict(body.metadata or {})}
    asset_metadata.update(
        {
            "category": "eval_output",
            "origin_type": "EVAL",
            "provider": provider,
            "generated_by": provider,
        }
    )
    if workflow is not None:
        asset_metadata["workflow"] = workflow
    asset.metadata_ = asset_metadata
    cell.eval_output_id = output.id
    cell.status = "succeeded"
    generated_at = output.generated_at.isoformat() if output.generated_at else None
    cell.output_snapshot = {
        "asset_id": asset.id,
        "asset_revision_id": asset.id,
        "workflow": effective_workflow,
        "provider": provider,
        "generated_at": generated_at,
        "source": "existing_asset",
    }
    all_cells = db.scalars(
        select(models.GridCell)
        .where(models.GridCell.grid_definition_id == grid.id)
        .order_by(models.GridCell.ordinal)
    ).all()
    if all_cells and all(item.status == "succeeded" and item.eval_output_id for item in all_cells):
        grid.status = "succeeded"
    elif grid.status == "succeeded":
        grid.status = "draft"

    if workflow and workflow.get("asset_id"):
        workflow_asset_id = str(workflow["asset_id"])
        edge = db.scalar(
            select(models.LineageEdge).where(
                models.LineageEdge.workspace_id == current_workspace(db).id,
                models.LineageEdge.source_type == "asset",
                models.LineageEdge.source_id == workflow_asset_id,
                models.LineageEdge.target_type == "asset",
                models.LineageEdge.target_id == asset.id,
                models.LineageEdge.relationship == "generated_by_workflow",
            )
        )
        if edge is None:
            db.add(
                models.LineageEdge(
                    workspace_id=current_workspace(db).id,
                    source_type="asset",
                    source_id=workflow_asset_id,
                    target_type="asset",
                    target_id=asset.id,
                    relationship="generated_by_workflow",
                    metadata_={"provider": provider, "workflow": workflow},
                )
            )

    actor = active_profile(db, x_profile_id)
    db.flush()
    result = _attachment_response(grid, cell, output, asset)
    result["generated_at"] = generated_at
    db.add(
        models.Job(
            workspace_id=current_workspace(db).id,
            kind="grid.attach_existing_asset",
            state=models.JobState.succeeded,
            profile_id=actor.id,
            idempotency_key=job_key,
            payload={
                "request": request_snapshot,
                "grid_definition_id": grid.id,
                "cell_id": cell.id,
                "eval_output_id": output.id,
            },
            progress=1.0,
            result=result,
        )
    )
    record_activity(
        db,
        action="grid.cell_attached",
        subject_type="grid_cell",
        subject_id=cell.id,
        profile_id=actor.id,
        project_id=project.id,
        details={
            "grid_id": grid.id,
            "asset_id": asset.id,
            "eval_output_id": output.id,
            "provider": provider,
        },
    )
    db.commit()
    return result


@router.post("/projects/{project_id}/experiment-plans/preflight")
def experiment_plan_preflight(project_id: str, body: schemas.PlanPreflightRequest, db: DB):
    _project(db, project_id)
    return _preflight(body, project_id, db)


@router.post("/projects/{project_id}/grids", status_code=status.HTTP_201_CREATED)
def create_grid(project_id: str, body: schemas.GridCreateRequest, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    project = _project(db, project_id)
    existing = db.scalar(select(models.GridDefinition).where(models.GridDefinition.project_id == project_id, models.GridDefinition.idempotency_key == body.idempotency_key))
    if existing:
        return _grid_response(db, existing)
    resolved = _resolve_body(body.plan, project_id, db)
    if not resolved["valid"]:
        raise HTTPException(status_code=422, detail={"code": "SCHEMA_INCOMPATIBLE", "errors": resolved["schema_errors"]})
    resolved_response = {
        "request_count": resolved["request_count"],
        "estimated_cost": resolved["estimated_cost"],
    }
    snapshot = resolved["plan"]
    plan = db.scalar(select(models.ExperimentPlan).where(models.ExperimentPlan.project_id == project_id, models.ExperimentPlan.digest == snapshot["digest"]))
    if plan is None:
        plan = models.ExperimentPlan(project_id=project_id, contract_version=CONTRACT_VERSION, plan_version=snapshot["plan_version"], digest=snapshot["digest"], snapshot=snapshot, idempotency_key=None)
        db.add(plan)
        db.flush()
    stored_snapshot = {**snapshot, "plan_id": plan.id}
    plan.snapshot = stored_snapshot
    grid = models.GridDefinition(
        project_id=project_id,
        name=body.name,
        eval_definition_id=None,
        x_axis=stored_snapshot["axes"]["x"],
        y_axis=stored_snapshot["axes"]["y"],
        z_axis=stored_snapshot["axes"].get("z"),
        plan_id=plan.id,
        plan_version=stored_snapshot["plan_version"],
        plan_digest=stored_snapshot["digest"],
        plan_snapshot=stored_snapshot,
        request_count=resolved_response["request_count"],
        estimated_cost=resolved_response["estimated_cost"],
        idempotency_key=body.idempotency_key,
    )
    db.add(grid)
    db.flush()
    for cell in resolved["cells"]:
        coordinate = cell["coordinate"]
        db.add(models.GridCell(
            grid_definition_id=grid.id,
            ordinal=cell["ordinal"],
            x_index=int(coordinate["x"] or 0),
            y_index=int(coordinate["y"] or 0),
            z_index=int(coordinate["z"]) if coordinate["z"] is not None else -1,
            coordinate=coordinate,
            case_snapshot=cell["case"],
            target_snapshot=cell["target"],
            endpoint_id=cell["target"]["endpoint_id"],
            endpoint_adapter=cell["target"]["provider"],
            schema_digest=cell["schema_digest"],
            effective_params=cell["effective_params"],
            status="pending",
            request_count=cell["request_count"],
            estimated_cost=cell["estimated_cost"],
            idempotency_key=f"{body.idempotency_key}:{cell['ordinal']}",
        ))
    actor = active_profile(db, x_profile_id)
    record_activity(db, action="grid.created", subject_type="grid_definition", subject_id=grid.id, profile_id=actor.id, project_id=project_id, details={"plan_digest": snapshot["digest"]})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(select(models.GridDefinition).where(models.GridDefinition.project_id == project_id, models.GridDefinition.idempotency_key == body.idempotency_key))
        if existing:
            return _grid_response(db, existing)
        raise HTTPException(status_code=409, detail="grid idempotency key already used") from exc
    return _grid_response(db, grid)


@router.get("/projects/{project_id}/grids")
def list_grids(project_id: str, db: DB):
    _project(db, project_id)
    rows = db.scalars(select(models.GridDefinition).where(models.GridDefinition.project_id == project_id).order_by(models.GridDefinition.created_at.desc())).all()
    return {"items": [_grid_response(db, row) for row in rows], "next_cursor": None}


@router.get("/projects/{project_id}/grids/{grid_id}")
def get_grid(project_id: str, grid_id: str, db: DB):
    _project(db, project_id)
    grid = get_or_404(db, models.GridDefinition, grid_id)
    if grid.project_id != project_id:
        raise HTTPException(status_code=404, detail="grid not found")
    return _grid_response(db, grid)


@router.post("/projects/{project_id}/grids/{grid_id}/queue", status_code=status.HTTP_202_ACCEPTED)
def queue_grid(project_id: str, grid_id: str, body: schemas.GridQueueRequest, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    project = _project(db, project_id)
    grid = get_or_404(db, models.GridDefinition, grid_id)
    if grid.project_id != project.id:
        raise HTTPException(status_code=404, detail="grid not found")
    if grid.plan_version != body.plan_version or grid.plan_digest != body.plan_digest:
        raise HTTPException(status_code=409, detail={"code": "PLAN_SNAPSHOT_MISMATCH", "plan_version": grid.plan_version, "plan_digest": grid.plan_digest})
    parent_key = f"grid:{project_id}:{grid_id}:{body.idempotency_key}"
    existing = db.scalar(select(models.Job).where(models.Job.idempotency_key == parent_key))
    if existing:
        payload = dict(existing.payload or {})
        return {"grid_id": grid.id, "run_id": payload.get("eval_run_id"), "admission_id": payload.get("admission_id"), "status": existing.state.value if hasattr(existing.state, "value") else existing.state, "plan_version": grid.plan_version, "plan_digest": grid.plan_digest, "request_count": len(payload.get("cell_ordinals") or []), "child_admissions": payload.get("child_admissions", [])}
    cells = list(db.scalars(select(models.GridCell).where(models.GridCell.grid_definition_id == grid.id).order_by(models.GridCell.ordinal)))
    if body.cell_ordinals is not None:
        requested = list(body.cell_ordinals)
        if len(requested) != len(set(requested)):
            raise HTTPException(status_code=422, detail={"code": "DUPLICATE_CELL_ORDINAL"})
        persisted_ordinals = {cell.ordinal for cell in cells}
        unknown = sorted(set(requested) - persisted_ordinals)
        if unknown:
            raise HTTPException(status_code=422, detail={"code": "UNKNOWN_CELL_ORDINAL", "ordinals": unknown})
        wanted = set(requested)
        cells = [cell for cell in cells if cell.ordinal in wanted]
    if not cells:
        raise HTTPException(status_code=422, detail="queue requires at least one cell")
    try:
        admission = build_admission_plan(kind="grid", subjects=[FalSubjectDraft(ordinal=index, kind="grid_cell", stable_key=cell.id, prompt_id=None, grid_cell_id=cell.id, input_digest=str((cell.case_snapshot or {}).get("input_digest", "0" * 64)).removeprefix("sha256:"), expected_output_count=1, admitted_ordinals=(0,)) for index, cell in enumerate(cells)])
    except FalAdmissionValidationError as exc:
        raise HTTPException(status_code=422, detail={"code": "ADMISSION_INVALID", "message": str(exc)}) from exc
    targets = {(str((cell.target_snapshot or {}).get("endpoint_id")), str((cell.target_snapshot or {}).get("model_version_id")), str((cell.target_snapshot or {}).get("checkpoint_revision_id"))) for cell in cells}
    singleton = len(targets) == 1
    primary_target = dict(cells[0].target_snapshot or {})
    version = db.get(models.ModelVersion, primary_target.get("model_version_id")) if primary_target.get("model_version_id") else None
    definition = models.EvalDefinition(project_id=project_id, name=f"{grid.name} generation", endpoint=primary_target["endpoint_id"] if singleton else "mixed", model_version_id=version.id if version and singleton else None, inline_prompts=[case for case in grid.plan_snapshot.get("cases", [])], parameters=dict(grid.plan_snapshot.get("shared_params") or {}), plan_id=grid.plan_id, plan_version=grid.plan_version, plan_digest=grid.plan_digest)
    db.add(definition)
    db.flush()
    run = models.EvalRun(definition_id=definition.id, plan_id=grid.plan_id, plan_version=grid.plan_version, plan_digest=grid.plan_digest, checkpoint_id=version.checkpoint_id if version and singleton else None, checkpoint_revision_id=version.checkpoint_revision_id if version and singleton else None, status="queued", parameters_snapshot=dict(grid.plan_snapshot.get("shared_params") or {}))
    db.add(run)
    db.flush()
    actor = active_profile(db, x_profile_id)
    job = models.Job(workspace_id=current_workspace(db).id, kind="fal.grid_batch", profile_id=actor.id, idempotency_key=parent_key, payload={})
    db.add(job)
    db.flush()
    queue_item = models.GenerationQueueItem(workspace_id=current_workspace(db).id, client_request_id=parent_key, workflow="grid", provider="fal", queue_owner="grid", project_id=project_id, model_id=version.model_id if version and singleton else None, model_version_ids=sorted({str((cell.target_snapshot or {})["model_version_id"]) for cell in cells if (cell.target_snapshot or {}).get("model_version_id")}), compiled_request_id=str(uuid4()), state="admitted", request_snapshot={"grid_definition_id": grid.id, "run_id": run.id, "billing_acknowledgement": body.billing_acknowledgement, "plan": grid.plan_snapshot, "cells": [{"grid_cell_id": cell.id, "ordinal": cell.ordinal, "target": cell.target_snapshot, "effective_params": cell.effective_params, "coordinate": cell.coordinate} for cell in cells]}, progress={"completed": 0, "total": len(cells), "fraction": 0.0}, result={}, job_id=job.id)
    db.add(queue_item)
    db.flush()
    child_admissions = []
    for index, cell in enumerate(cells):
        child = models.GenerationQueueChild(queue_item_id=queue_item.id, ordinal=index, model_version_id=(str((cell.target_snapshot or {})["model_version_id"]) if (cell.target_snapshot or {}).get("model_version_id") else None), compiled_request_id=str(uuid4()), state="admitted", progress={}, result={"grid_cell_id": cell.id})
        db.add(child)
        db.flush()
        cell.status = "admitted"
        child_admissions.append({"ordinal": index, "cell_id": cell.id, "admission_id": child.id, "status": child.state})
    job.payload = {"grid_definition_id": grid.id, "eval_run_id": run.id, "queue_item_id": queue_item.id, "admission_id": queue_item.id, "billing_acknowledgement": body.billing_acknowledgement, "cell_ordinals": [cell.ordinal for cell in cells], "child_admissions": child_admissions, "expected_artifact_count": admission.expected_artifact_count}
    grid.status = "queued"
    record_activity(db, action="grid.queued", subject_type="grid_definition", subject_id=grid.id, profile_id=actor.id, project_id=project_id, details={"job_id": job.id, "admission_id": queue_item.id, "billing_acknowledgement": body.billing_acknowledgement})
    db.commit()
    return {"grid_id": grid.id, "run_id": run.id, "admission_id": queue_item.id, "status": "queued", "plan_version": grid.plan_version, "plan_digest": grid.plan_digest, "request_count": len(cells), "child_admissions": child_admissions}


@router.get("/projects/{project_id}/evals")
def list_evals(project_id: str, db: DB):
    _project(db, project_id)
    rows = db.scalars(select(models.EvalAssessment).where(models.EvalAssessment.project_id == project_id).order_by(models.EvalAssessment.created_at.desc())).all()
    return {"items": [{**_dump(row), "plan_version": row.plan_version, "plan_digest": row.plan_digest} for row in rows], "next_cursor": None}


@router.post("/projects/{project_id}/evals", status_code=status.HTTP_201_CREATED)
def create_eval(project_id: str, body: schemas.EvalCreateRequest, db: DB):
    _project(db, project_id)
    kind = str(body.assessment.get("kind") or "human_review")
    if kind not in {"human_review", "image_quality"}:
        raise HTTPException(status_code=422, detail={"code": "UNSUPPORTED_ASSESSMENT", "supported": ["human_review", "image_quality"]})
    run = get_or_404(db, models.EvalRun, body.run_id)
    definition = get_or_404(db, models.EvalDefinition, run.definition_id)
    if definition.project_id != project_id:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status != "succeeded":
        raise HTTPException(status_code=409, detail="assessment requires a completed generation run")
    if not run.plan_id or not run.plan_digest:
        raise HTTPException(status_code=409, detail="generation run has no immutable experiment plan")
    plan = get_or_404(db, models.ExperimentPlan, run.plan_id)
    if plan.project_id != project_id or plan.digest != run.plan_digest:
        raise HTTPException(status_code=409, detail="generation run plan snapshot is invalid")
    existing = db.scalar(select(models.EvalAssessment).where(models.EvalAssessment.project_id == project_id, models.EvalAssessment.idempotency_key == body.idempotency_key))
    if existing:
        return _assessment_response(db, existing)
    assessment = models.EvalAssessment(project_id=project_id, name=body.name, run_id=run.id, plan_id=plan.id, plan_version=plan.plan_version, plan_digest=plan.digest, assessment=dict(body.assessment), status="ready_for_review", idempotency_key=body.idempotency_key)
    db.add(assessment)
    db.commit()
    return _assessment_response(db, assessment)


def _assessment_response(db: Session, assessment: models.EvalAssessment) -> dict[str, Any]:
    reviews = db.scalars(select(models.Review).where(models.Review.subject_type == "eval_run", models.Review.subject_id == assessment.run_id).order_by(models.Review.created_at.desc())).all()
    return {**_dump(assessment), "plan_version": assessment.plan_version, "plan_digest": assessment.plan_digest, "reviews": [_dump(review) for review in reviews]}


@router.get("/projects/{project_id}/evals/{eval_id}")
def get_eval(project_id: str, eval_id: str, db: DB):
    _project(db, project_id)
    assessment = get_or_404(db, models.EvalAssessment, eval_id)
    if assessment.project_id != project_id:
        raise HTTPException(status_code=404, detail="eval not found")
    return _assessment_response(db, assessment)
