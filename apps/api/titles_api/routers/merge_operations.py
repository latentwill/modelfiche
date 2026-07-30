from __future__ import annotations

from datetime import timezone
from hashlib import sha256
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..checkpoint_revisions import establish_checkpoint_revision
from ..database import get_db
from ..merge_operations import canonical_recipe, compact_notation, merge_projection, recipe_digest
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..settings import get_settings

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def _project(db: Session, project_id: str) -> models.Project:
    project = get_or_404(db, models.Project, project_id)
    if project.workspace_id != current_workspace(db).id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _output_directory(operation_id: str) -> Path:
    return (get_settings().asset_root / "merges" / operation_id).resolve()


def _safe_output_path(operation_id: str, value: str) -> Path:
    root = _output_directory(operation_id)
    path = Path(value).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"merge output must be inside {root}") from exc
    return path


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_input(db: Session, workspace_id: str, item: schemas.MergeRecipeInput) -> models.CheckpointRevision:
    revision = get_or_404(db, models.CheckpointRevision, item.checkpoint_revision_id)
    checkpoint = get_or_404(db, models.Checkpoint, revision.checkpoint_id)
    run = get_or_404(db, models.TrainingRun, checkpoint.run_id)
    source_project = get_or_404(db, models.Project, run.project_id)
    if source_project.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail=f"merge input {item.alias} was not found")
    asset = get_or_404(db, models.Asset, revision.asset_id)
    recorded_sha = (asset.sha256 or "").lower()
    if recorded_sha != item.sha256.lower():
        raise HTTPException(status_code=409, detail=f"merge input {item.alias} SHA-256 does not match checkpoint revision evidence")
    return revision


def _operation_for_run(db: Session, run_id: str) -> models.MergeOperation:
    operation = db.scalar(select(models.MergeOperation).where(models.MergeOperation.run_id == run_id))
    if operation is None:
        raise HTTPException(status_code=404, detail="Merge operation not found")
    return operation


@router.post("/merge-operations/prepare", status_code=status.HTTP_201_CREATED)
def prepare_merge(
    body: schemas.MergePrepareRequest,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    project = _project(db, body.project_id)
    canonical = canonical_recipe(body.recipe)
    digest = recipe_digest(body.recipe)
    existing = db.scalar(
        select(models.MergeOperation).where(
            models.MergeOperation.project_id == project.id,
            models.MergeOperation.recipe_digest == digest,
        )
    )
    if existing:
        output_dir = _output_directory(existing.id)
        output_dir.mkdir(parents=True, exist_ok=True)
        return merge_projection(db, existing, output_directory=str(output_dir))

    revisions = [_verified_input(db, project.workspace_id, item) for item in body.recipe.inputs]
    registration = body.registration.model_dump(mode="json", exclude_none=True)
    run = models.TrainingRun(
        project_id=project.id,
        dataset_version_id=None,
        name=body.recipe.name,
        trainer="checkpoint-merge",
        run_kind="checkpoint_merge",
        base_model=body.recipe.base_model,
        status="preparing",
        raw_manifest={
            "operation": "checkpoint_merge",
            "schema": body.recipe.schema_version,
            "recipe": canonical,
            "recipe_digest": digest,
            "registration": registration,
            "client_request_id": body.client_request_id,
        },
        raw_state={"status": "preparing", "phase": "preparing"},
        normalized_config={
            "trainer": "checkpoint-merge",
            "operator": body.recipe.resolved_operator,
            "lora_rank": body.recipe.output.rank,
            "dtype": body.recipe.output.dtype,
            "merge_recipe": canonical,
        },
    )
    db.add(run)
    db.flush()
    operation = models.MergeOperation(
        workspace_id=current_workspace(db).id,
        project_id=project.id,
        run_id=run.id,
        schema_version=body.recipe.schema_version,
        operator=body.recipe.resolved_operator,
        base_model=body.recipe.base_model,
        status="preparing",
        recipe=canonical,
        recipe_digest=digest,
        compact_notation=compact_notation(body.recipe),
        output_rank=body.recipe.output.rank,
        dtype=body.recipe.output.dtype,
        last_heartbeat_at=models.utcnow(),
    )
    db.add(operation)
    db.flush()
    for position, (item, revision) in enumerate(zip(body.recipe.inputs, revisions, strict=True)):
        merge_input = models.MergeInput(
            merge_operation_id=operation.id,
            checkpoint_revision_id=revision.id,
            alias=item.alias,
            role=item.role,
            source_rank=item.rank,
            position=position,
        )
        db.add(merge_input)
        db.flush()
        for scope, value in sorted(item.weights.items()):
            module_pattern = scope.removeprefix("module:") if scope.startswith("module:") else None
            normalized_scope = "module_pattern" if module_pattern else scope
            db.add(models.MergeInputWeight(
                merge_input_id=merge_input.id,
                scope=normalized_scope,
                module_pattern=module_pattern,
                weight=value,
            ))
    record_activity(
        db,
        action="merge.prepared",
        subject_type="merge_operation",
        subject_id=operation.id,
        profile_id=active_profile(db, x_profile_id).id,
        project_id=project.id,
        details={"run_id": run.id, "recipe_digest": digest, "operator": operation.operator},
    )
    output_dir = _output_directory(operation.id)
    output_dir.mkdir(parents=True, exist_ok=True)
    db.commit()
    return merge_projection(db, operation, output_directory=str(output_dir))


@router.get("/merge-operations/{operation_id}")
def get_merge(operation_id: str, db: DB):
    operation = get_or_404(db, models.MergeOperation, operation_id)
    _project(db, operation.project_id)
    return merge_projection(db, operation, output_directory=str(_output_directory(operation.id)))


@router.get("/runs/{run_id}/merge")
def merge_for_run(run_id: str, db: DB):
    operation = _operation_for_run(db, run_id)
    _project(db, operation.project_id)
    return merge_projection(db, operation, output_directory=str(_output_directory(operation.id)))


@router.post("/merge-operations/{operation_id}/heartbeat")
def heartbeat_merge(operation_id: str, body: schemas.MergeHeartbeatRequest, db: DB):
    operation = get_or_404(db, models.MergeOperation, operation_id)
    _project(db, operation.project_id)
    if operation.status == "completed":
        return merge_projection(db, operation)
    operation.last_heartbeat_at = models.utcnow()
    operation.status = "running"
    run = get_or_404(db, models.TrainingRun, operation.run_id)
    run.status = "running"
    run.raw_state = {**dict(run.raw_state or {}), "status": "running", "phase": body.phase or "merging"}
    db.commit()
    return merge_projection(db, operation)


@router.post("/merge-operations/{operation_id}/resume")
def resume_merge(operation_id: str, db: DB):
    operation = get_or_404(db, models.MergeOperation, operation_id)
    _project(db, operation.project_id)
    if operation.status == "completed":
        return merge_projection(db, operation, output_directory=str(_output_directory(operation.id)))
    operation.status = "preparing"
    operation.error_code = None
    operation.error_message = None
    operation.failure_phase = None
    operation.last_heartbeat_at = models.utcnow()
    run = get_or_404(db, models.TrainingRun, operation.run_id)
    run.status = "preparing"
    run.raw_state = {"status": "preparing", "phase": "resume"}
    db.commit()
    return merge_projection(db, operation, output_directory=str(_output_directory(operation.id)))


@router.post("/merge-operations/{operation_id}/fail")
def fail_merge(
    operation_id: str,
    body: schemas.MergeFailRequest,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    operation = get_or_404(db, models.MergeOperation, operation_id)
    _project(db, operation.project_id)
    if operation.status == "completed":
        raise HTTPException(status_code=409, detail="completed merge operations cannot be failed")
    operation.status = "failed"
    operation.failure_phase = body.phase
    operation.error_code = body.error_code
    operation.error_message = body.error_message
    operation.last_heartbeat_at = models.utcnow()
    run = get_or_404(db, models.TrainingRun, operation.run_id)
    run.status = "failed"
    run.raw_state = {"status": "failed", "phase": body.phase, "error_code": body.error_code, "error": body.error_message}
    record_activity(
        db,
        action="merge.failed",
        subject_type="merge_operation",
        subject_id=operation.id,
        profile_id=active_profile(db, x_profile_id).id,
        project_id=operation.project_id,
        details={"phase": body.phase, "error_code": body.error_code},
    )
    db.commit()
    return merge_projection(db, operation)


@router.post("/merge-operations/{operation_id}/complete")
def complete_merge(
    operation_id: str,
    body: schemas.MergeCompleteRequest,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    operation = get_or_404(db, models.MergeOperation, operation_id)
    _project(db, operation.project_id)
    if operation.status == "completed":
        projection = merge_projection(db, operation)
        if str(projection.get("output_sha256") or "").lower() != body.output_sha256.lower():
            raise HTTPException(status_code=409, detail="merge operation already completed with a different output")
        return projection
    if operation.status not in {"preparing", "running", "failed"}:
        raise HTTPException(status_code=409, detail=f"merge operation cannot complete from {operation.status}")

    output_path = _safe_output_path(operation.id, body.output_path)
    if not output_path.is_file():
        raise HTTPException(status_code=400, detail="merge output file does not exist")
    actual_size = output_path.stat().st_size
    if actual_size != body.output_size:
        raise HTTPException(status_code=409, detail=f"merge output size mismatch: expected {body.output_size}, found {actual_size}")
    actual_sha = _sha256(output_path)
    if actual_sha.lower() != body.output_sha256.lower():
        raise HTTPException(status_code=409, detail="merge output SHA-256 mismatch")

    workspace = current_workspace(db)
    now = models.utcnow()
    asset = models.Asset(
        workspace_id=workspace.id,
        project_id=operation.project_id,
        kind=models.AssetKind.model,
        name=output_path.name,
        mime_type="application/octet-stream",
        sha256=actual_sha,
        provenance_kind="generated",
        metadata_={
            "category": "model_checkpoint",
            "merge_operation_id": operation.id,
            "recipe_digest": operation.recipe_digest,
            "operator": operation.operator,
            "output_rank": operation.output_rank,
            "dtype": operation.dtype,
        },
    )
    db.add(asset)
    db.flush()
    location = models.AssetLocation(
        asset_id=asset.id,
        workspace_id=workspace.id,
        provider="local",
        uri=output_path.as_uri(),
        size=actual_size,
        verified_size=actual_size,
        verified_sha256=actual_sha,
        verification_state="verified",
        hydration_state="hydrated",
        modified_at=now,
        last_verified_at=now,
        last_seen_at=now,
    )
    db.add(location)
    db.flush()
    asset.preferred_location_id = location.id
    asset.origin_location_id = location.id
    checkpoint = models.Checkpoint(run_id=operation.run_id, step=body.output_step, asset_id=asset.id, state="available")
    db.add(checkpoint)
    db.flush()
    revision = establish_checkpoint_revision(db, checkpoint, location=location)
    if revision is None:
        raise HTTPException(status_code=500, detail="failed to establish output checkpoint revision")

    inputs = db.scalars(select(models.MergeInput).where(models.MergeInput.merge_operation_id == operation.id)).all()
    for merge_input in inputs:
        existing_edge = db.scalar(select(models.LineageEdge).where(
            models.LineageEdge.workspace_id == workspace.id,
            models.LineageEdge.source_type == "checkpoint_revision",
            models.LineageEdge.source_id == merge_input.checkpoint_revision_id,
            models.LineageEdge.target_type == "checkpoint_revision",
            models.LineageEdge.target_id == revision.id,
            models.LineageEdge.relationship == "merged_into",
        ))
        if existing_edge is None:
            db.add(models.LineageEdge(
                workspace_id=workspace.id,
                source_type="checkpoint_revision",
                source_id=merge_input.checkpoint_revision_id,
                target_type="checkpoint_revision",
                target_id=revision.id,
                relationship="merged_into",
                metadata_={"merge_operation_id": operation.id, "operator": operation.operator},
            ))

    run = get_or_404(db, models.TrainingRun, operation.run_id)
    registration = dict((run.raw_manifest or {}).get("registration") or {})
    model_version = None
    if registration.get("model_id"):
        model = get_or_404(db, models.Model, str(registration["model_id"]))
        if model.project_id != operation.project_id:
            raise HTTPException(status_code=400, detail="merge output model belongs to a different project")
        model_version = models.ModelVersion(
            model_id=model.id,
            checkpoint_id=checkpoint.id,
            checkpoint_revision_id=revision.id,
            name=str(registration["version_name"]),
            trigger_words=list(registration.get("trigger_words") or []),
            base_model=operation.base_model,
            notes=registration.get("notes"),
            lifecycle_state="candidate",
            readiness={},
        )
        db.add(model_version)
        db.flush()
    else:
        model = models.Model(
            project_id=operation.project_id,
            name=str(operation.recipe.get("name") or "Merged Krea model"),
            description="Model created from a managed checkpoint merge.",
        )
        db.add(model)
        db.flush()
        model_version = models.ModelVersion(
            model_id=model.id,
            checkpoint_id=checkpoint.id,
            checkpoint_revision_id=revision.id,
            name=str(registration.get("version_name") or operation.recipe.get("name") or "Merged checkpoint"),
            trigger_words=list(registration.get("trigger_words") or []),
            base_model=operation.base_model,
            notes=registration.get("notes"),
            lifecycle_state="candidate",
            readiness={},
        )
        db.add(model_version)
        db.flush()

    operation.status = "completed"
    operation.output_checkpoint_revision_id = revision.id
    operation.error_code = None
    operation.error_message = None
    operation.failure_phase = None
    operation.last_heartbeat_at = now
    run.status = "completed"
    run.finished_at = now.astimezone(timezone.utc)
    run.raw_state = {"status": "completed", "phase": "completed", "usable_output": True, "output_sha256": actual_sha}
    record_activity(
        db,
        action="merge.completed",
        subject_type="merge_operation",
        subject_id=operation.id,
        profile_id=active_profile(db, x_profile_id).id,
        project_id=operation.project_id,
        details={
            "run_id": run.id,
            "checkpoint_id": checkpoint.id,
            "checkpoint_revision_id": revision.id,
            "model_version_id": model_version.id if model_version else None,
            "output_sha256": actual_sha,
        },
    )
    db.commit()
    return merge_projection(db, operation)
