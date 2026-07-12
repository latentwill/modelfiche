from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..checkpoint_revisions import establish_checkpoint_revision
from ..database import get_db
from ..services import active_profile, get_or_404, record_activity

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def dump(row) -> dict[str, Any]:
    return {column.name: getattr(row, column.key) for column in row.__table__.columns}


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


@router.get("/runs")
def runs(db: DB, project_id: str | None = None, run_status: str | None = Query(None, alias="status")):
    query = select(models.TrainingRun).order_by(models.TrainingRun.created_at.desc(), models.TrainingRun.id.desc())
    if project_id:
        query = query.where(models.TrainingRun.project_id == project_id)
    if run_status:
        query = query.where(models.TrainingRun.status == run_status)
    rows = []
    for row in db.scalars(query).all():
        project = db.get(models.Project, row.project_id)
        result = db.execute(
            select(models.Model, models.ModelVersion)
            .join(models.ModelVersion, models.ModelVersion.model_id == models.Model.id)
            .join(models.Checkpoint, models.Checkpoint.id == models.ModelVersion.checkpoint_id)
            .where(models.Checkpoint.run_id == row.id)
            .order_by(models.Checkpoint.step.desc(), models.ModelVersion.created_at.desc(), models.ModelVersion.id.desc())
        ).first()
        result_model, result_version = result if result else (None, None)
        rows.append({
            **dump(row),
            "project_name": project.title if project else None,
            "checkpoint_count": int(db.scalar(select(func.count()).select_from(models.Checkpoint).where(models.Checkpoint.run_id == row.id)) or 0),
            "sample_count": int(db.scalar(select(func.count()).select_from(models.Sample).where(models.Sample.run_id == row.id)) or 0),
            "result_model_id": result_model.id if result_model else None,
            "result_model_name": result_model.name if result_model else None,
            "result_model_version_id": result_version.id if result_version else None,
            "result_model_version_name": result_version.name if result_version else None,
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
    record_activity(db, action="run.created", subject_type="run", subject_id=run.id, profile_id=active_profile(db, x_profile_id).id, project_id=run.project_id)
    db.commit()
    return dump(run)


@router.get("/runs/{run_id}")
def run(run_id: str, db: DB):
    run = get_or_404(db, models.TrainingRun, run_id)
    checkpoints = db.scalars(select(models.Checkpoint).where(models.Checkpoint.run_id == run.id).order_by(models.Checkpoint.step)).all()
    dataset_version = db.get(models.DatasetVersion, run.dataset_version_id) if run.dataset_version_id else None
    checkpoint_rows = []
    for checkpoint in checkpoints:
        asset = db.get(models.Asset, checkpoint.asset_id)
        location = db.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == checkpoint.asset_id).order_by(models.AssetLocation.created_at.desc()))
        version = db.scalar(select(models.ModelVersion).where(models.ModelVersion.checkpoint_id == checkpoint.id))
        checkpoint_rows.append({
            **dump(checkpoint),
            "asset_revision_id": checkpoint.asset_id,
            "filename": asset.name if asset else None,
            "size": location.size if location else None,
            "hydration_state": location.hydration_state if location else None,
            "model_version_name": version.name if version else None,
        })
    dataset = db.get(models.Dataset, dataset_version.dataset_id) if dataset_version else None
    stages = db.scalars(
        select(models.TrainingStage)
        .where(models.TrainingStage.run_id == run.id)
        .order_by(models.TrainingStage.created_at)
    ).all()
    return {
        **dump(run),
        **{key: value for key, value in dict(run.normalized_config or {}).items() if key in {"learning_rate", "lora_rank", "steps", "epochs", "usable_output", "source_status", "source_message", "status_evidence"}},
        "dataset_id": dataset.id if dataset else None,
        "dataset_name": dataset.name if dataset else None,
        "dataset_version_name": dataset_version.name if dataset_version else None,
        "stages": [dump(stage) for stage in stages],
        "checkpoints": checkpoint_rows,
        "sample_count": int(db.scalar(select(func.count()).select_from(models.Sample).where(models.Sample.run_id == run.id)) or 0),
        "metric_count": int(db.scalar(select(func.count()).select_from(models.TrainingMetric).where(models.TrainingMetric.run_id == run.id)) or 0),
    }

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
    return [{**dump(c), "asset_revision_id": c.asset_id} for c in db.scalars(select(models.Checkpoint).where(models.Checkpoint.run_id == run_id).order_by(models.Checkpoint.step)).all()]


@router.post("/runs/{run_id}/samples", status_code=status.HTTP_201_CREATED)
def create_sample(run_id: str, body: schemas.SampleCreate, db: DB):
    get_or_404(db, models.TrainingRun, run_id)
    get_or_404(db, models.Asset, body.asset_id)
    location = db.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == body.asset_id).order_by(models.AssetLocation.modified_at.desc()))
    values = body.model_dump()
    if values["checkpoint_id"] is None and values["step"] is not None:
        checkpoint = db.scalar(select(models.Checkpoint).where(models.Checkpoint.run_id == run_id, models.Checkpoint.step == values["step"]))
        values["checkpoint_id"] = checkpoint.id if checkpoint else None
    if values["checkpoint_id"] is not None:
        checkpoint = get_or_404(db, models.Checkpoint, values["checkpoint_id"])
        if checkpoint.run_id != run_id:
            raise HTTPException(status_code=400, detail="sample checkpoint does not belong to training run")
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
    for sample in db.scalars(query.offset(offset).limit(limit)).all():
        asset = db.get(models.Asset, sample.asset_id)
        review = db.scalar(select(models.Review).where(models.Review.subject_type == "asset", models.Review.subject_id == sample.asset_id).order_by(models.Review.created_at.desc()))
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
        })
    return rows


@router.get("/checkpoints/{checkpoint_id}")
def checkpoint(checkpoint_id: str, db: DB):
    checkpoint = get_or_404(db, models.Checkpoint, checkpoint_id)
    asset = db.get(models.Asset, checkpoint.asset_id)
    locations = db.scalars(select(models.AssetLocation).where(models.AssetLocation.asset_id == checkpoint.asset_id)).all()
    run = db.get(models.TrainingRun, checkpoint.run_id)
    asset_row = None if asset is None else {
        "asset_revision_id": asset.id,
        "id": asset.id, "project_id": asset.project_id, "kind": asset.kind,
        "name": asset.name, "mime_type": asset.mime_type, "sha256": asset.sha256,
        "metadata": asset.metadata_, "created_at": asset.created_at, "updated_at": asset.updated_at,
    }
    return {**dump(checkpoint), "run_name": run.name if run else None, "run_id": checkpoint.run_id, "asset": asset_row, "locations": [dump(location) for location in locations], "artifact": checkpoint_artifact(db, checkpoint)}


@router.post("/checkpoints/{checkpoint_id}/hydrate", status_code=status.HTTP_202_ACCEPTED)
def hydrate_checkpoint(checkpoint_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    checkpoint = get_or_404(db, models.Checkpoint, checkpoint_id)
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
    query = select(models.Model).order_by(models.Model.updated_at.desc())
    if project_id:
        query = query.where(models.Model.project_id == project_id)
    rows = []
    for model in db.scalars(query).all():
        project = db.get(models.Project, model.project_id)
        latest = db.scalar(select(models.ModelVersion).where(models.ModelVersion.model_id == model.id).order_by(models.ModelVersion.created_at.desc()))
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
    versions = db.scalars(select(models.ModelVersion).where(models.ModelVersion.model_id == model.id).order_by(models.ModelVersion.created_at.desc())).all()
    version_rows = []
    for version in versions:
        checkpoint = db.get(models.Checkpoint, version.checkpoint_id)
        artifact = checkpoint_artifact(db, checkpoint)
        version_rows.append({
            **dump(version),
            "checkpoint_step": checkpoint.step if checkpoint else None,
            "checkpoint_run_id": checkpoint.run_id if checkpoint else None,
            "artifact": artifact,
            "filename": artifact["filename"] if artifact else None,
            "asset_id": artifact["asset_id"] if artifact else None,
            "storage": artifact["storage"] if artifact else [],
            "fal_status": version.readiness.get("fal_status") if version.readiness else None,
            "fal_url": version.readiness.get("fal_url") if version.readiness else None,
        })
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


@router.post("/model-versions", status_code=status.HTTP_201_CREATED)
def create_model_version(body: schemas.ModelVersionCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    model = get_or_404(db, models.Model, body.model_id)
    checkpoint = get_or_404(db, models.Checkpoint, body.checkpoint_id)
    run = get_or_404(db, models.TrainingRun, checkpoint.run_id)
    if run.project_id != model.project_id:
        raise HTTPException(status_code=400, detail="checkpoint and model must belong to the same project")
    version = models.ModelVersion(**body.model_dump())
    version.checkpoint_revision_id = checkpoint.current_revision_id
    db.add(version)
    db.flush()
    record_activity(db, action="model_version.registered", subject_type="model_version", subject_id=version.id, profile_id=active_profile(db, x_profile_id).id, project_id=model.project_id, details={"checkpoint_id": checkpoint.id, "step": checkpoint.step})
    db.commit()
    return dump(version)


@router.get("/model-versions")
def model_versions(db: DB, model_id: str | None = None, project_id: str | None = None, lifecycle_state: str | None = None):
    query = select(models.ModelVersion).order_by(models.ModelVersion.created_at.desc())
    if model_id:
        query = query.where(models.ModelVersion.model_id == model_id)
    if lifecycle_state:
        query = query.where(models.ModelVersion.lifecycle_state == lifecycle_state)
    if project_id:
        query = query.join(models.Model, models.Model.id == models.ModelVersion.model_id).where(models.Model.project_id == project_id)
    rows = []
    for version in db.scalars(query).all():
        model = db.get(models.Model, version.model_id)
        checkpoint = db.get(models.Checkpoint, version.checkpoint_id)
        artifact = checkpoint_artifact(db, checkpoint)
        rows.append({
            **dump(version),
            "model_name": model.name if model else None,
            "project_id": model.project_id if model else None,
            "checkpoint_step": checkpoint.step if checkpoint else None,
            "checkpoint_run_id": checkpoint.run_id if checkpoint else None,
            "artifact": artifact,
            "filename": artifact["filename"] if artifact else None,
            "asset_id": artifact["asset_id"] if artifact else None,
            "storage": artifact["storage"] if artifact else [],
            "fal_status": version.readiness.get("fal_status") if version.readiness else None,
            "fal_url": version.readiness.get("fal_url") if version.readiness else None,
        })
    return rows


@router.get("/model-versions/{version_id}")
def model_version(version_id: str, db: DB):
    version = get_or_404(db, models.ModelVersion, version_id)
    model = get_or_404(db, models.Model, version.model_id)
    checkpoint = get_or_404(db, models.Checkpoint, version.checkpoint_id)
    artifact = checkpoint_artifact(db, checkpoint)
    return {
        **dump(version),
        "model_name": model.name,
        "project_id": model.project_id,
        "checkpoint_step": checkpoint.step,
        "checkpoint_run_id": checkpoint.run_id,
        "artifact": artifact,
        "filename": artifact["filename"] if artifact else None,
        "asset_id": artifact["asset_id"] if artifact else None,
        "storage": artifact["storage"] if artifact else [],
        "fal_status": version.readiness.get("fal_status") if version.readiness else None,
        "fal_url": version.readiness.get("fal_url") if version.readiness else None,
    }


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
