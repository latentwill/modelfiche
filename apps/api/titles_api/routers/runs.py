import asyncio
import json
from datetime import datetime
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from .. import models, schemas
from ..checkpoint_revisions import establish_checkpoint_revision
from ..database import get_db
from ..merge_operations import merge_projection
from ..services import active_profile, current_workspace, get_or_404, record_activity

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def dump(row) -> dict[str, Any]:
    return {column.name: getattr(row, column.key) for column in row.__table__.columns}

def _live_projection(db: Session, run: models.TrainingRun) -> dict[str, Any]:
    summaries = db.scalars(
        select(models.RunMetricSummary)
        .where(models.RunMetricSummary.run_id == run.id)
        .order_by(models.RunMetricSummary.name)
    ).all()
    summary_rows = {
        row.name: {
            "name": row.name,
            "step": row.last_step,
            "value": row.last_value,
            "value_text": row.last_value_text,
            "value_type": row.value_type,
            "minimum": row.minimum,
            "maximum": row.maximum,
            "updated_at": row.updated_at,
        }
        for row in summaries
    }
    loss = next(
        (summary_rows[name] for name in ("loss", "loss/denoise") if name in summary_rows),
        next((value for name, value in summary_rows.items() if "loss" in name.casefold()), None),
    )
    learning_rate = next(
        (summary_rows[name] for name in ("learning_rate", "lr") if name in summary_rows),
        next((value for name, value in summary_rows.items() if "learning" in name.casefold() and "rate" in name.casefold()), None),
    )
    upload_counts = {
        state: count
        for state, count in db.execute(
            select(models.RunUpload.state, func.count())
            .where(models.RunUpload.run_id == run.id)
            .group_by(models.RunUpload.state)
        )
    }
    finish = run.finished_at or models.utcnow()
    elapsed_seconds = max(0.0, (finish - run.started_at).total_seconds()) if run.started_at else None
    return {
        "run_id": run.id,
        "status": run.status,
        "current_step": run.current_step,
        "latest_loss": loss,
        "learning_rate": learning_rate,
        "elapsed_seconds": elapsed_seconds,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "last_event_at": run.last_event_at,
        "summaries": list(summary_rows.values()),
        "uploads": upload_counts,
        "sample_count": int(
            db.scalar(select(func.count()).select_from(models.Sample).where(models.Sample.run_id == run.id)) or 0
        ),
    }

def sample_provenance(db: Session, sample: models.Sample, asset: models.Asset | None) -> tuple[int | None, str | None]:
    checkpoint = db.get(models.Checkpoint, sample.checkpoint_id) if sample.checkpoint_id else None
    training_step = checkpoint.step if checkpoint else None
    if training_step is None and asset:
        match = re.search(r"__(\d{9})_\d+\.[^.]+$", asset.name)
        if match:
            training_step = int(match.group(1))
    if training_step is None:
        training_step = sample.step
    locations = db.scalars(select(models.AssetLocation).where(models.AssetLocation.asset_id == sample.asset_id)).all()
    object_keys = " ".join(location.object_key or "" for location in locations).lower()
    track = "flat" if "/kzapata_flat_" in object_keys else "render" if "/kzapata_render_" in object_keys else None
    return training_step, track


def model_version_chronology(db: Session, version: models.ModelVersion) -> tuple[float, int, float]:
    checkpoint = db.get(models.Checkpoint, version.checkpoint_id)
    run = db.get(models.TrainingRun, checkpoint.run_id) if checkpoint else None
    source_time = ((run.raw_manifest or {}).get("created_at") or (run.raw_state or {}).get("updated_at")) if run else None
    try:
        parsed = datetime.fromisoformat(str(source_time).replace("Z", "+00:00")).timestamp() if source_time else run.created_at.timestamp() if run else 0.0
    except (TypeError, ValueError):
        parsed = run.created_at.timestamp() if run else 0.0
    return parsed, checkpoint.step if checkpoint else -1, version.created_at.timestamp()


def checkpoint_artifact(db: Session, checkpoint: models.Checkpoint | None) -> dict[str, Any] | None:
    """Stable, explicit checkpoint artifact projection; never substitutes dataset identity."""
    if checkpoint is None:
        return None
    asset = db.get(models.Asset, checkpoint.asset_id)
    locations = db.scalars(
        select(models.AssetLocation)
        .where(models.AssetLocation.asset_id == checkpoint.asset_id)
        .order_by(models.AssetLocation.created_at.desc())
    ).all()
    revision = db.get(models.CheckpointRevision, checkpoint.current_revision_id) if checkpoint.current_revision_id else None
    return {
        "checkpoint_id": checkpoint.id,
        "checkpoint_revision_id": revision.id if revision else None,
        "asset_id": checkpoint.asset_id,
        "asset_revision_id": checkpoint.asset_id,
        "filename": asset.name if asset else None,
        "mime_type": asset.mime_type if asset else None,
        "sha256": asset.sha256 if asset else None,
        "step": checkpoint.step,
        "state": checkpoint.state,
        "metadata": asset.metadata_ if asset else {},
        "storage": [
            {
                "location_id": location.id,
                "provider": location.provider,
                "uri": location.uri,
                "bucket": location.bucket,
                "object_key": location.object_key,
                "relative_path": location.relative_path,
                "size": location.size,
                "hydration_state": location.hydration_state,
                "verification_state": location.verification_state,
            }
            for location in locations
        ],
    }


def run_dataset_inputs(db: Session, run: models.TrainingRun | None) -> list[dict[str, Any]]:
    if run is None:
        return []
    inputs = db.scalars(
        select(models.TrainingRunDatasetInput)
        .where(models.TrainingRunDatasetInput.run_id == run.id)
        .order_by(models.TrainingRunDatasetInput.position, models.TrainingRunDatasetInput.id)
    ).all()
    rows = []
    for item in inputs:
        version = db.get(models.DatasetVersion, item.dataset_version_id)
        dataset = db.get(models.Dataset, version.dataset_id) if version else None
        subset = db.get(models.DatasetVersionSubset, item.subset_id) if item.subset_id else None
        rows.append({
            **dump(item),
            "dataset_id": dataset.id if dataset else None,
            "dataset_name": dataset.name if dataset else None,
            "dataset_version_name": version.name if version else None,
            "subset_key": subset.key if subset else None,
            "subset_name": subset.name if subset else None,
            "color_token": subset.color_token if subset else "blue",
            "available": bool(dataset and version and (not item.subset_id or subset)),
        })
    return rows


def run_dataset_link(db: Session, run: models.TrainingRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    inputs = run_dataset_inputs(db, run)
    if inputs:
        first = inputs[0]
        return {
            "dataset_id": first["dataset_id"],
            "dataset_name": first["dataset_name"],
            "dataset_version_id": first["dataset_version_id"],
            "dataset_version_name": first["dataset_version_name"],
            "available": first["available"],
            "input_count": len(inputs),
        }
    if not run.dataset_version_id:
        return None
    version = db.get(models.DatasetVersion, run.dataset_version_id)
    dataset = db.get(models.Dataset, version.dataset_id) if version else None
    return {
        "dataset_id": dataset.id if dataset else None,
        "dataset_name": dataset.name if dataset else None,
        "dataset_version_id": run.dataset_version_id,
        "dataset_version_name": version.name if version else None,
        "available": bool(dataset and version),
        "input_count": 1,
    }


def run_merge_summary(db: Session, run: models.TrainingRun) -> dict[str, Any] | None:
    operation = db.scalar(select(models.MergeOperation).where(models.MergeOperation.run_id == run.id))
    return merge_projection(db, operation) if operation else None


def checkpoint_readiness(
    db: Session,
    checkpoint: models.Checkpoint,
    version: models.ModelVersion | None = None,
) -> dict[str, Any]:
    artifact = checkpoint_artifact(db, checkpoint) or {}
    storage = artifact.get("storage") if isinstance(artifact.get("storage"), list) else []
    local = next(
        (
            location
            for location in storage
            if str(location.get("provider", "")).lower() == "local"
            and str(location.get("hydration_state", "")).lower() in {"hydrated", "available", "present"}
        ),
        None,
    )
    remote = next(
        (
            location
            for location in storage
            if str(location.get("provider", "")).lower() != "local"
            and bool(location.get("uri") or location.get("object_key"))
        ),
        None,
    )
    hydration_job = db.scalar(
        select(models.Job)
        .where(
            models.Job.kind == "checkpoint.hydrate",
            models.Job.payload["checkpoint_id"].as_string() == checkpoint.id,
            models.Job.state.in_([models.JobState.queued, models.JobState.running]),
        )
        .order_by(models.Job.created_at.desc())
    )
    checkpoint_state = str(checkpoint.state or "").lower()
    hydrated = bool(local) or checkpoint_state == "hydrated"
    local_status = "hydrated" if hydrated else "hydrating" if hydration_job else "failed" if checkpoint_state == "failed" else "remote" if remote else "missing"
    can_hydrate = bool(remote and not hydrated and hydration_job is None and checkpoint_state != "failed")
    if hydrated:
        reason = "A verified local copy is already available."
    elif hydration_job:
        reason = "Hydration is already queued or running."
    elif checkpoint_state == "failed":
        reason = "The checkpoint is failed; repair its source before hydration."
    elif not remote:
        reason = "No remote checkpoint location is available."
    else:
        reason = "Review the artifact size and source before downloading a local copy."

    raw_readiness = dict(version.readiness or {}) if version else {}
    fal_url = raw_readiness.get("fal_url") or raw_readiness.get("fal_path")
    endpoint_id = raw_readiness.get("endpoint_id") or raw_readiness.get("endpoint") or raw_readiness.get("fal_endpoint")
    fal_status = raw_readiness.get("fal_status") or ("ready" if fal_url and endpoint_id else "registered" if fal_url else "not_registered")
    return {
        "local": {
            "status": local_status,
            "available": hydrated,
            "can_hydrate": can_hydrate,
            "reason": reason,
            "path": local.get("relative_path") or local.get("uri") if local else None,
            "verification_state": local.get("verification_state") if local else None,
            "job_id": hydration_job.id if hydration_job else None,
        },
        "remote": {
            "available": bool(remote),
            "provider": remote.get("provider") if remote else None,
            "size": remote.get("size") if remote else None,
            "location": remote.get("object_key") or remote.get("uri") if remote else None,
        },
        "fal": {
            "status": fal_status,
            "available": bool(fal_url and endpoint_id),
            "url": fal_url,
            "endpoint_id": endpoint_id,
        },
    }


def checkpoint_projection(db: Session, checkpoint: models.Checkpoint) -> dict[str, Any]:
    artifact = checkpoint_artifact(db, checkpoint)
    version = db.scalar(
        select(models.ModelVersion)
        .where(models.ModelVersion.checkpoint_id == checkpoint.id)
        .order_by(models.ModelVersion.created_at.desc())
    )
    model = db.get(models.Model, version.model_id) if version else None
    run = db.get(models.TrainingRun, checkpoint.run_id)
    registered_version = None if version is None else {
        "id": version.id,
        "name": version.name,
        "model_id": version.model_id,
        "model_name": model.name if model else None,
        "lifecycle_state": version.lifecycle_state,
        "checkpoint_revision_id": version.checkpoint_revision_id,
    }
    return {
        **dump(checkpoint),
        "artifact": artifact,
        "asset_revision_id": checkpoint.asset_id,
        "filename": artifact["filename"] if artifact else None,
        "size": next((location.get("size") for location in artifact["storage"] if location.get("size") is not None), None) if artifact else None,
        "registered_version": registered_version,
        "model_version_id": version.id if version else None,
        "model_version_name": version.name if version else None,
        "readiness_summary": checkpoint_readiness(db, checkpoint, version),
        "dataset": run_dataset_link(db, run),
    }


def sample_identity(sample: models.Sample, asset: models.Asset | None) -> tuple[str, str, int | None]:
    filename = asset.name if asset else ""
    match = re.search(r"__(\d{9})_(\d+)\.[^.]+$", filename)
    if match:
        ordinal = int(match.group(2))
        return f"sample-{ordinal}", sample.prompt or f"Sample {ordinal + 1}", ordinal
    if sample.prompt:
        seed = "" if sample.seed is None else str(sample.seed)
        return f"prompt:{sample.prompt}\x1fseed:{seed}", sample.prompt, None
    return f"sample-record:{sample.id}", asset.name if asset else "Unlabelled sample", None


def _run_preview_asset_ids(db: Session, run_ids: set[str]) -> dict[str, str]:
    """Batch-resolve latest image samples, then image checkpoints as a safe fallback."""
    if not run_ids:
        return {}
    previews: dict[str, str] = {}
    sample_rows = db.execute(
        select(models.Sample.run_id, models.Asset.id)
        .join(models.Asset, models.Asset.id == models.Sample.asset_id)
        .where(models.Sample.run_id.in_(run_ids), models.Asset.kind == models.AssetKind.image)
        .order_by(
            models.Sample.step.desc().nulls_last(),
            models.Sample.modified_at.desc().nulls_last(),
            models.Sample.created_at.desc(),
            models.Sample.id.desc(),
        )
    ).all()
    for run_id, asset_id in sample_rows:
        previews.setdefault(run_id, asset_id)
    checkpoint_rows = db.execute(
        select(models.Checkpoint.run_id, models.Asset.id)
        .join(models.Asset, models.Asset.id == models.Checkpoint.asset_id)
        .where(models.Checkpoint.run_id.in_(run_ids), models.Asset.kind == models.AssetKind.image)
        .order_by(models.Checkpoint.step.desc(), models.Checkpoint.created_at.desc(), models.Checkpoint.id.desc())
    ).all()
    for run_id, asset_id in checkpoint_rows:
        previews.setdefault(run_id, asset_id)
    return previews


@router.get("/runs")
def runs(
    db: DB,
    project_id: str | None = None,
    run_status: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=1000),
    include_archived: bool = False,
):
    query = (
        select(models.TrainingRun)
        .join(models.Project, models.Project.id == models.TrainingRun.project_id)
        .where(models.Project.workspace_id == current_workspace(db).id)
        .order_by(models.TrainingRun.created_at.desc(), models.TrainingRun.id.desc())
        .limit(limit)
    )
    if project_id:
        query = query.where(models.TrainingRun.project_id == project_id)
    if run_status:
        query = query.where(models.TrainingRun.status == run_status)
    if not include_archived:
        query = query.where(models.TrainingRun.archived_at.is_(None))
    run_rows = db.scalars(query).all()
    previews = _run_preview_asset_ids(db, {row.id for row in run_rows})
    rows = []
    for row in run_rows:
        live = _live_projection(db, row)
        project = db.get(models.Project, row.project_id)
        result = db.execute(
            select(models.Model, models.ModelVersion)
            .join(models.ModelVersion, models.ModelVersion.model_id == models.Model.id)
            .join(models.Checkpoint, models.Checkpoint.id == models.ModelVersion.checkpoint_id)
            .where(models.Checkpoint.run_id == row.id)
            .order_by(models.Checkpoint.step.desc(), models.ModelVersion.created_at.desc(), models.ModelVersion.id.desc())
        ).first()
        result_model, result_version = result if result else (None, None)
        dataset = run_dataset_link(db, row)
        rows.append({
            **dump(row),
            "project_name": project.title if project else None,
            "dataset_id": dataset["dataset_id"] if dataset else None,
            "dataset_name": dataset["dataset_name"] if dataset else None,
            "dataset_version_name": dataset["dataset_version_name"] if dataset else None,
            "dataset_linkage": dataset,
            "dataset_inputs": run_dataset_inputs(db, row),
            "merge_summary": run_merge_summary(db, row),
            "checkpoint_count": int(db.scalar(select(func.count()).select_from(models.Checkpoint).where(models.Checkpoint.run_id == row.id)) or 0),
            "sample_count": int(db.scalar(select(func.count()).select_from(models.Sample).where(models.Sample.run_id == row.id)) or 0),
            "result_model_id": result_model.id if result_model else None,
            "result_model_name": result_model.name if result_model else None,
            "result_model_version_id": result_version.id if result_version else None,
            "result_model_version_name": result_version.name if result_version else None,
            "preview_asset_id": previews.get(row.id),
            "latest_loss": live["latest_loss"],
            "learning_rate": live["learning_rate"],
            "elapsed_seconds": live["elapsed_seconds"],
            "last_event_at": live["last_event_at"],
            "upload_status": live["uploads"],
        })
    return rows


@router.post("/runs", status_code=status.HTTP_201_CREATED)
def create_run(body: schemas.RunCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    get_or_404(db, models.Project, body.project_id)
    if body.dataset_version_id:
        get_or_404(db, models.DatasetVersion, body.dataset_version_id)
    run = models.TrainingRun(**body.model_dump())
    db.add(run)
    db.flush()
    if body.dataset_version_id:
        version = get_or_404(db, models.DatasetVersion, body.dataset_version_id)
        item_count = int(db.scalar(select(func.count()).select_from(models.DatasetItem).where(
            models.DatasetItem.dataset_version_id == version.id,
            models.DatasetItem.included.is_(True),
        )) or 0)
        db.add(models.TrainingRunDatasetInput(
            run_id=run.id,
            dataset_version_id=version.id,
            item_count_snapshot=item_count,
            sampling_weight=1.0,
            repeat_count=1,
            position=0,
            dataset_content_digest=version.content_digest,
        ))
    record_activity(db, action="run.created", subject_type="run", subject_id=run.id, profile_id=active_profile(db, x_profile_id).id, project_id=run.project_id)
    db.commit()
    return dump(run)


@router.get("/runs/{run_id}/dataset-inputs")
def dataset_inputs(run_id: str, db: DB):
    return run_dataset_inputs(db, get_or_404(db, models.TrainingRun, run_id))


@router.post("/runs/{run_id}/dataset-inputs", status_code=status.HTTP_201_CREATED)
def add_dataset_input(run_id: str, body: schemas.TrainingRunDatasetInputCreate, db: DB):
    run = get_or_404(db, models.TrainingRun, run_id)
    if run.status not in {"unknown", "pending", "preparing"}:
        raise HTTPException(status_code=409, detail="dataset composition is immutable after a run starts")
    version = get_or_404(db, models.DatasetVersion, body.dataset_version_id)
    dataset = get_or_404(db, models.Dataset, version.dataset_id)
    if dataset.project_id != run.project_id:
        raise HTTPException(status_code=400, detail="dataset input belongs to a different project")
    subset = get_or_404(db, models.DatasetVersionSubset, body.subset_id) if body.subset_id else None
    if subset and subset.dataset_version_id != version.id:
        raise HTTPException(status_code=400, detail="sub-dataset belongs to a different dataset version")
    existing = db.scalar(select(models.TrainingRunDatasetInput).where(
        models.TrainingRunDatasetInput.run_id == run.id,
        models.TrainingRunDatasetInput.dataset_version_id == version.id,
        models.TrainingRunDatasetInput.subset_id == body.subset_id,
    ))
    if existing:
        raise HTTPException(status_code=409, detail="dataset input is already attached to this run")
    item_count = body.item_count_snapshot
    if not item_count:
        if subset:
            item_count = int(db.scalar(
                select(func.count())
                .select_from(models.DatasetSubsetItem)
                .join(models.DatasetItem, models.DatasetItem.id == models.DatasetSubsetItem.dataset_item_id)
                .where(models.DatasetSubsetItem.subset_id == subset.id, models.DatasetItem.included.is_(True))
            ) or 0)
        else:
            item_count = int(db.scalar(select(func.count()).select_from(models.DatasetItem).where(
                models.DatasetItem.dataset_version_id == version.id,
                models.DatasetItem.included.is_(True),
            )) or 0)
    row = models.TrainingRunDatasetInput(
        run_id=run.id,
        **body.model_dump(exclude={"item_count_snapshot", "dataset_content_digest"}),
        item_count_snapshot=item_count,
        dataset_content_digest=body.dataset_content_digest or version.content_digest,
    )
    db.add(row)
    db.commit()
    return dump(row)


@router.patch("/runs/{run_id}/dataset-inputs/{input_id}")
def update_dataset_input(run_id: str, input_id: str, body: schemas.TrainingRunDatasetInputUpdate, db: DB):
    run = get_or_404(db, models.TrainingRun, run_id)
    row = get_or_404(db, models.TrainingRunDatasetInput, input_id)
    if row.run_id != run.id:
        raise HTTPException(status_code=404, detail="dataset input was not found")
    if run.status not in {"unknown", "pending", "preparing"}:
        raise HTTPException(status_code=409, detail="dataset composition is immutable after a run starts")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    return dump(row)


@router.delete("/runs/{run_id}/dataset-inputs/{input_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dataset_input(run_id: str, input_id: str, db: DB):
    run = get_or_404(db, models.TrainingRun, run_id)
    row = get_or_404(db, models.TrainingRunDatasetInput, input_id)
    if row.run_id != run.id:
        raise HTTPException(status_code=404, detail="dataset input was not found")
    if run.status not in {"unknown", "pending", "preparing"}:
        raise HTTPException(status_code=409, detail="dataset composition is immutable after a run starts")
    db.delete(row)
    db.commit()


@router.post("/runs/{run_id}/archive")
def archive_run(
    run_id: str,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    run = get_or_404(db, models.TrainingRun, run_id)
    if run.status not in {"completed", "failed", "interrupted", "canceled"}:
        raise HTTPException(status_code=409, detail="only terminal training runs can be archived")
    run.archived_at = models.utcnow()
    record_activity(
        db,
        action="run.archived",
        subject_type="run",
        subject_id=run.id,
        profile_id=active_profile(db, x_profile_id).id,
        project_id=run.project_id,
    )
    db.commit()
    return dump(run)


@router.post("/runs/{run_id}/restore")
def restore_run(
    run_id: str,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    run = get_or_404(db, models.TrainingRun, run_id)
    run.archived_at = None
    record_activity(
        db,
        action="run.restored",
        subject_type="run",
        subject_id=run.id,
        profile_id=active_profile(db, x_profile_id).id,
        project_id=run.project_id,
    )
    db.commit()
    return dump(run)


@router.get("/runs/{run_id}")
def run(run_id: str, db: DB):
    run = get_or_404(db, models.TrainingRun, run_id)
    checkpoints = db.scalars(select(models.Checkpoint).where(models.Checkpoint.run_id == run.id).order_by(models.Checkpoint.step)).all()
    dataset = run_dataset_link(db, run)
    stages = db.scalars(
        select(models.TrainingStage)
        .where(models.TrainingStage.run_id == run.id)
        .order_by(models.TrainingStage.created_at)
    ).all()
    return {
        **dump(run),
        **{key: value for key, value in dict(run.normalized_config or {}).items() if key in {"learning_rate", "lora_rank", "steps", "epochs", "usable_output", "source_status", "source_message", "status_evidence"}},
        "dataset_id": dataset["dataset_id"] if dataset else None,
        "dataset_name": dataset["dataset_name"] if dataset else None,
        "dataset_version_name": dataset["dataset_version_name"] if dataset else None,
        "dataset_linkage": dataset,
        "dataset_inputs": run_dataset_inputs(db, run),
        "merge_summary": run_merge_summary(db, run),
        "stages": [dump(stage) for stage in stages],
        "checkpoints": [checkpoint_projection(db, checkpoint) for checkpoint in checkpoints],
        "sample_count": int(db.scalar(select(func.count()).select_from(models.Sample).where(models.Sample.run_id == run.id)) or 0),
        "metric_count": int(db.scalar(select(func.count()).select_from(models.TrainingMetric).where(models.TrainingMetric.run_id == run.id)) or 0),
    }

@router.get("/runs/{run_id}/live")
def run_live(run_id: str, db: DB):
    return _live_projection(db, get_or_404(db, models.TrainingRun, run_id))


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    db: DB,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
):
    get_or_404(db, models.TrainingRun, run_id)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    try:
        requested_sequence = max(0, int(last_event_id or 0))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an event sequence") from exc

    async def stream():
        last_sequence = requested_sequence
        sent_snapshot = False
        keepalive_ticks = 0
        while not await request.is_disconnected():
            terminal = False
            messages: list[str] = []
            with factory() as session:
                current = session.get(models.TrainingRun, run_id)
                if current is None:
                    break
                projection = _live_projection(session, current)
                terminal = current.status in {"completed", "failed", "interrupted"}
                if not sent_snapshot:
                    latest_sequence = session.scalar(
                        select(func.max(models.RunEvent.sequence)).where(models.RunEvent.run_id == run_id)
                    ) or 0
                    last_sequence = max(last_sequence, latest_sequence)
                    payload = json.dumps(projection, default=str, separators=(",", ":"))
                    messages.append(f"id: {last_sequence}\nevent: run.snapshot\ndata: {payload}\n\n")
                    sent_snapshot = True
                events = session.scalars(
                    select(models.RunEvent)
                    .where(models.RunEvent.run_id == run_id, models.RunEvent.sequence > last_sequence)
                    .order_by(models.RunEvent.sequence)
                ).all()
                for event in events:
                    payload = json.dumps(
                        {"event": dump(event), "run": projection},
                        default=str,
                        separators=(",", ":"),
                    )
                    messages.append(f"id: {event.sequence}\nevent: run.update\ndata: {payload}\n\n")
                    last_sequence = event.sequence
            if messages:
                for message in messages:
                    yield message
                keepalive_ticks = 0
            elif terminal:
                break
            else:
                keepalive_ticks += 1
                if keepalive_ticks >= 15:
                    yield ": keepalive\n\n"
                    keepalive_ticks = 0
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/metrics")
def run_metrics(run_id: str, db: DB, name: str = Query("loss", min_length=1, max_length=120)):
    get_or_404(db, models.TrainingRun, run_id)
    metrics = db.scalars(
        select(models.TrainingMetric)
        .where(models.TrainingMetric.run_id == run_id, models.TrainingMetric.name == name)
        .order_by(models.TrainingMetric.step, models.TrainingMetric.id)
    ).all()
    return {
        "run_id": run_id,
        "metric_name": name,
        "points": [
            {"step": metric.step, "value": metric.value, "wall_time": metric.wall_time}
            for metric in metrics
        ],
        "final_value": metrics[-1].value if metrics else None,
        "source_key": metrics[-1].source_key if metrics else None,
    }


@router.get("/runs/{run_id}/config")
def run_config(run_id: str, db: DB, raw: bool = False):
    run = get_or_404(db, models.TrainingRun, run_id)
    result = {
        "run_id": run.id,
        "normalized": run.normalized_config,
        "raw": {"manifest": run.raw_manifest, "state": run.raw_state},
    }
    if raw:
        result.update({"manifest": run.raw_manifest, "state": run.raw_state})
    return result


@router.post("/runs/{run_id}/refresh", status_code=status.HTTP_202_ACCEPTED)
def refresh_run(run_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    run = get_or_404(db, models.TrainingRun, run_id)
    if not run.source_prefix:
        raise HTTPException(status_code=409, detail="run has no remote source prefix")
    source = None
    if run.origin_source_id:
        source = db.scalar(
            select(models.ImportSource).where(
                models.ImportSource.id == run.origin_source_id,
                models.ImportSource.is_active.is_(True),
            )
        )
    if source is None:
        source = db.scalar(select(models.ImportSource).where(models.ImportSource.is_active.is_(True)).order_by(models.ImportSource.created_at))
    if source is None:
        raise HTTPException(status_code=409, detail="no active import source is available")
    actor = active_profile(db, x_profile_id)
    job = models.Job(workspace_id=get_or_404(db, models.Project, run.project_id).workspace_id, kind="s3.import", profile_id=actor.id, payload={"source_id": source.id, "project_id": run.project_id, "prefix": run.source_prefix, "profile_id": actor.id, "hydrate_dataset_images": False})
    db.add(job)
    db.flush()
    import_job = models.ImportJob(source_id=source.id, project_id=run.project_id, job_id=job.id, prefix=run.source_prefix, detected_type="run", state="queued")
    db.add(import_job)
    record_activity(db, action="run.refresh_queued", subject_type="run", subject_id=run.id, profile_id=actor.id, project_id=run.project_id, details={"job_id": job.id, "import_job_id": import_job.id})
    db.commit()
    return {"run_id": run.id, "job_id": job.id, "import_job_id": import_job.id, "state": "queued"}


@router.post("/runs/{run_id}/checkpoints", status_code=status.HTTP_201_CREATED)
def create_checkpoint(run_id: str, body: schemas.CheckpointCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    run = get_or_404(db, models.TrainingRun, run_id)
    asset = get_or_404(db, models.Asset, body.asset_id)
    if asset.kind != models.AssetKind.model:
        raise HTTPException(status_code=400, detail="checkpoint asset must have kind=model")
    checkpoint = models.Checkpoint(run_id=run.id, **body.model_dump())
    db.add(checkpoint)
    db.flush()
    establish_checkpoint_revision(db, checkpoint)
    record_activity(db, action="checkpoint.registered", subject_type="checkpoint", subject_id=checkpoint.id, profile_id=active_profile(db, x_profile_id).id, project_id=run.project_id, details={"step": checkpoint.step})
    db.commit()
    return dump(checkpoint)


@router.get("/runs/{run_id}/checkpoints")
def checkpoints(run_id: str, db: DB):
    get_or_404(db, models.TrainingRun, run_id)
    return [
        checkpoint_projection(db, checkpoint)
        for checkpoint in db.scalars(
            select(models.Checkpoint)
            .where(models.Checkpoint.run_id == run_id)
            .order_by(models.Checkpoint.step)
        ).all()
    ]


@router.post("/runs/{run_id}/samples", status_code=status.HTTP_201_CREATED)
def create_sample(run_id: str, body: schemas.SampleCreate, db: DB):
    run = get_or_404(db, models.TrainingRun, run_id)
    asset = get_or_404(db, models.Asset, body.asset_id)
    if asset.project_id is not None and asset.project_id != run.project_id:
        raise HTTPException(status_code=409, detail="sample asset belongs to another project")
    location = db.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == body.asset_id).order_by(models.AssetLocation.modified_at.desc()))
    values = body.model_dump()
    if values["checkpoint_id"] is None and values["step"] is not None:
        checkpoint = db.scalar(select(models.Checkpoint).where(models.Checkpoint.run_id == run_id, models.Checkpoint.step == values["step"]))
        values["checkpoint_id"] = checkpoint.id if checkpoint else None
    if values["checkpoint_id"] is not None:
        checkpoint = get_or_404(db, models.Checkpoint, values["checkpoint_id"])
        if checkpoint.run_id != run_id:
            raise HTTPException(status_code=400, detail="sample checkpoint does not belong to training run")
    asset.project_id = run.project_id
    asset.metadata_ = {**dict(asset.metadata_ or {}), "category": "sample"}
    sample = models.Sample(run_id=run_id, modified_at=location.modified_at if location else None, **values)
    db.add(sample)
    db.commit()
    return dump(sample)


@router.get("/runs/{run_id}/samples")
def samples(run_id: str, db: DB, step: int | None = None, limit: int = Query(200, le=1000), offset: int = 0):
    get_or_404(db, models.TrainingRun, run_id)
    query = select(models.Sample).where(models.Sample.run_id == run_id).order_by(models.Sample.modified_at.asc().nulls_last(), models.Sample.id)
    if step is not None:
        query = query.where(models.Sample.step == step)
    rows = []
    checkpoint_cache: dict[str, dict[str, Any]] = {}
    step_checkpoint_cache: dict[tuple[int, str | None], models.Checkpoint | None] = {}
    for sample in db.scalars(query.offset(offset).limit(limit)).all():
        asset = db.get(models.Asset, sample.asset_id)
        review = db.scalar(select(models.Review).where(models.Review.subject_type == "asset", models.Review.subject_id == sample.asset_id).order_by(models.Review.created_at.desc()))
        training_step, sample_track = sample_provenance(db, sample, asset)
        identity, identity_label, identity_ordinal = sample_identity(sample, asset)
        profile = db.get(models.UserProfile, review.profile_id) if review else None
        checkpoint = db.get(models.Checkpoint, sample.checkpoint_id) if sample.checkpoint_id else None
        if checkpoint is None and training_step is not None:
            cache_key = (training_step, sample_track)
            if cache_key not in step_checkpoint_cache:
                candidates = db.execute(
                    select(models.Checkpoint, models.Asset)
                    .join(models.Asset, models.Asset.id == models.Checkpoint.asset_id)
                    .where(models.Checkpoint.run_id == run_id, models.Checkpoint.step == training_step)
                ).all()
                matching = [
                    candidate
                    for candidate in candidates
                    if sample_track and sample_track in str(candidate[1].name or "").lower()
                ]
                step_checkpoint_cache[cache_key] = (matching or candidates)[0][0] if (matching or candidates) else None
            checkpoint = step_checkpoint_cache[cache_key]
        checkpoint_row = None
        if checkpoint:
            checkpoint_row = checkpoint_cache.get(checkpoint.id)
            if checkpoint_row is None:
                checkpoint_row = checkpoint_projection(db, checkpoint)
                checkpoint_cache[checkpoint.id] = checkpoint_row
        rows.append({
            **dump(sample),
            "asset_revision_id": sample.asset_id,
            "filename": asset.name if asset else None,
            "name": asset.name if asset else None,
            "mime_type": asset.mime_type if asset else None,
            "rating": review.rating if review else None,
            "decision": review.decision if review else None,
            "origin_type": "SAMPLE",
            "modified_at": sample.modified_at,
            "generated_at": None,
            "training_step": training_step,
            "sample_identity": identity,
            "sample_label": identity_label,
            "sample_ordinal": identity_ordinal,
            "sample_track": sample_track,
            "review_id": review.id if review else None,
            "reviewed_by": profile.display_name if profile else None,
            "reviewed_at": review.created_at if review else None,
            "checkpoint_id": checkpoint.id if checkpoint else sample.checkpoint_id,
            "checkpoint_filename": checkpoint_row["filename"] if checkpoint_row else None,
            "checkpoint_state": checkpoint.state if checkpoint else None,
            "checkpoint_readiness": checkpoint_row["readiness_summary"] if checkpoint_row else None,
            "model_version_id": checkpoint_row["model_version_id"] if checkpoint_row else None,
            "model_version_name": checkpoint_row["model_version_name"] if checkpoint_row else None,
        })
    return rows


@router.get("/checkpoints/{checkpoint_id}")
def checkpoint(checkpoint_id: str, db: DB):
    checkpoint = get_or_404(db, models.Checkpoint, checkpoint_id)
    run = db.get(models.TrainingRun, checkpoint.run_id)
    projection = checkpoint_projection(db, checkpoint)
    return {
        **projection,
        "run_name": run.name if run else None,
        "run_id": checkpoint.run_id,
        "asset": None if projection["artifact"] is None else {
            "asset_revision_id": projection["artifact"]["asset_id"],
            "id": projection["artifact"]["asset_id"],
            "project_id": run.project_id if run else None,
            "kind": "model",
            "name": projection["artifact"]["filename"],
            "mime_type": projection["artifact"]["mime_type"],
            "sha256": projection["artifact"]["sha256"],
            "metadata": projection["artifact"]["metadata"],
        },
    }


@router.post("/checkpoints/{checkpoint_id}/hydrate", status_code=status.HTTP_202_ACCEPTED)
def hydrate_checkpoint(checkpoint_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    checkpoint = get_or_404(db, models.Checkpoint, checkpoint_id)
    readiness = checkpoint_readiness(db, checkpoint)
    if not readiness["local"]["can_hydrate"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=readiness["local"]["reason"])
    run = get_or_404(db, models.TrainingRun, checkpoint.run_id)
    existing = db.scalar(select(models.Job).where(
        models.Job.kind == "checkpoint.hydrate",
        models.Job.payload["checkpoint_id"].as_string() == checkpoint.id,
        models.Job.state.in_([models.JobState.queued, models.JobState.running]),
    ).order_by(models.Job.created_at.desc()))
    if existing:
        return dump(existing)
    job = models.Job(workspace_id=get_or_404(db, models.Project, run.project_id).workspace_id, kind="checkpoint.hydrate", profile_id=active_profile(db, x_profile_id).id, payload={"checkpoint_id": checkpoint.id})
    db.add(job)
    db.flush()
    record_activity(db, action="checkpoint.hydration_queued", subject_type="checkpoint", subject_id=checkpoint.id, profile_id=job.profile_id, project_id=run.project_id, details={"job_id": job.id})
    db.commit()
    return dump(job)


@router.get("/models")
def models_list(db: DB, project_id: str | None = None):
    query = select(models.Model).join(models.Project, models.Project.id == models.Model.project_id).where(
        models.Project.workspace_id == current_workspace(db).id,
    ).order_by(models.Model.updated_at.desc())
    if project_id:
        query = query.where(models.Model.project_id == project_id)
    rows = []
    for model in db.scalars(query).all():
        project = db.get(models.Project, model.project_id)
        versions = db.scalars(select(models.ModelVersion).where(models.ModelVersion.model_id == model.id)).all()
        latest = max(versions, key=lambda version: model_version_chronology(db, version), default=None)
        rows.append({
            **dump(model),
            "project_name": project.title if project else None,
            "latest_version": latest.name if latest else None,
            "lifecycle_state": latest.lifecycle_state if latest else None,
            "base_model": latest.base_model if latest else None,
            "version_count": int(db.scalar(select(func.count()).select_from(models.ModelVersion).where(models.ModelVersion.model_id == model.id)) or 0),
        })
    return rows


@router.get("/models/{model_id}")
def model_detail(model_id: str, db: DB):
    model = get_or_404(db, models.Model, model_id)
    versions = sorted(
        db.scalars(select(models.ModelVersion).where(models.ModelVersion.model_id == model.id)).all(),
        key=lambda version: model_version_chronology(db, version),
        reverse=True,
    )
    version_rows = [model_version_projection(db, version) for version in versions]
    return {**dump(model), "versions": version_rows}


@router.post("/models", status_code=status.HTTP_201_CREATED)
def create_model(body: schemas.ModelCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    get_or_404(db, models.Project, body.project_id)
    model = models.Model(**body.model_dump())
    db.add(model)
    db.flush()
    record_activity(db, action="model.created", subject_type="model", subject_id=model.id, profile_id=active_profile(db, x_profile_id).id, project_id=model.project_id)
    db.commit()
    return dump(model)


def model_version_projection(db: Session, version: models.ModelVersion) -> dict[str, Any]:
    model = db.get(models.Model, version.model_id)
    checkpoint = db.get(models.Checkpoint, version.checkpoint_id)
    run = db.get(models.TrainingRun, checkpoint.run_id) if checkpoint else None
    artifact = checkpoint_artifact(db, checkpoint)
    return {
        **dump(version),
        "model_name": model.name if model else None,
        "project_id": model.project_id if model else None,
        "checkpoint_step": checkpoint.step if checkpoint else None,
        "checkpoint_run_id": checkpoint.run_id if checkpoint else None,
        "run_name": run.name if run else None,
        "artifact": artifact,
        "filename": artifact["filename"] if artifact else None,
        "asset_id": artifact["asset_id"] if artifact else None,
        "storage": artifact["storage"] if artifact else [],
        "fal_status": version.readiness.get("fal_status") if version.readiness else None,
        "fal_url": version.readiness.get("fal_url") if version.readiness else None,
        "readiness_summary": checkpoint_readiness(db, checkpoint, version) if checkpoint else None,
        "dataset": run_dataset_link(db, run),
    }


@router.post("/model-versions", status_code=status.HTTP_201_CREATED)
def create_model_version(body: schemas.ModelVersionCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    model = get_or_404(db, models.Model, body.model_id)
    checkpoint = get_or_404(db, models.Checkpoint, body.checkpoint_id)
    run = get_or_404(db, models.TrainingRun, checkpoint.run_id)
    if run.project_id != model.project_id:
        raise HTTPException(status_code=400, detail="checkpoint and model must belong to the same project")
    existing = db.scalar(select(models.ModelVersion).where(models.ModelVersion.checkpoint_id == checkpoint.id))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"checkpoint is already registered as model version {existing.name}")
    version = models.ModelVersion(**body.model_dump())
    version.checkpoint_revision_id = checkpoint.current_revision_id
    db.add(version)
    db.flush()
    record_activity(db, action="model_version.registered", subject_type="model_version", subject_id=version.id, profile_id=active_profile(db, x_profile_id).id, project_id=model.project_id, details={"checkpoint_id": checkpoint.id, "step": checkpoint.step})
    db.commit()
    return dump(version)


@router.get("/model-versions")
def model_versions(db: DB, model_id: str | None = None, project_id: str | None = None, lifecycle_state: str | None = None):
    query = select(models.ModelVersion).join(models.Model, models.Model.id == models.ModelVersion.model_id).join(
        models.Project, models.Project.id == models.Model.project_id,
    ).where(models.Project.workspace_id == current_workspace(db).id).order_by(models.ModelVersion.created_at.desc())
    if model_id:
        query = query.where(models.ModelVersion.model_id == model_id)
    if lifecycle_state:
        query = query.where(models.ModelVersion.lifecycle_state == lifecycle_state)
    if project_id:
        query = query.where(models.Model.project_id == project_id)
    return [model_version_projection(db, version) for version in db.scalars(query).all()]


@router.get("/model-versions/{version_id}")
def model_version(version_id: str, db: DB):
    version = get_or_404(db, models.ModelVersion, version_id)
    return model_version_projection(db, version)


@router.post("/model-versions/{version_id}/approve")
def approve_model_version(version_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    return update_model_version_state(version_id, schemas.ModelVersionState(lifecycle_state="approved"), db, x_profile_id)


@router.post("/model-versions/{version_id}/archive")
def archive_model_version(version_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    return update_model_version_state(version_id, schemas.ModelVersionState(lifecycle_state="archived"), db, x_profile_id)


@router.patch("/model-versions/{version_id}/state")
def update_model_version_state(version_id: str, body: schemas.ModelVersionState, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    version = get_or_404(db, models.ModelVersion, version_id)
    model = get_or_404(db, models.Model, version.model_id)
    version.lifecycle_state = body.lifecycle_state
    record_activity(db, action=f"model_version.{body.lifecycle_state}", subject_type="model_version", subject_id=version.id, profile_id=active_profile(db, x_profile_id).id, project_id=model.project_id)
    db.commit()
    return dump(version)
