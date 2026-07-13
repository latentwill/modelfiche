from typing import Annotated, Any
from collections import defaultdict

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import active_profile, current_workspace, get_or_404, record_activity

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def dump(row) -> dict[str, Any]:
    return {column.name: getattr(row, column.key) for column in row.__table__.columns}


def attributed(db: Session, row) -> dict[str, Any]:
    profile = db.get(models.UserProfile, row.profile_id) if row.profile_id else None
    return {
        **dump(row),
        "subject": f"{row.subject_type}:{row.subject_id}",
        "profile_name": profile.display_name if profile else None,
    }


def _lineage_preview(db: Session, edge: models.LineageEdge) -> str | None:
    for kind, identifier in ((edge.target_type, edge.target_id), (edge.source_type, edge.source_id)):
        if kind == "eval_output":
            output = db.get(models.EvalOutput, identifier)
            if output:
                return output.asset_id
        if kind == "checkpoint":
            asset_id = db.scalar(select(models.Sample.asset_id).where(models.Sample.checkpoint_id == identifier).order_by(models.Sample.created_at.desc()).limit(1))
            if asset_id:
                return asset_id
        if kind == "training_run":
            asset_id = db.scalar(select(models.Sample.asset_id).where(models.Sample.run_id == identifier).order_by(models.Sample.created_at.desc()).limit(1))
            if asset_id:
                return asset_id
        if kind == "asset":
            return identifier
    return None


@router.post("/reviews", status_code=status.HTTP_201_CREATED)
def create_review(body: schemas.ReviewCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    if body.rating is None and body.decision is None:
        raise HTTPException(status_code=422, detail="rating or decision is required")
    actor = active_profile(db, body.profile_id or x_profile_id)
    review = models.Review(workspace_id=current_workspace(db).id, profile_id=actor.id, **body.model_dump(exclude={"profile_id"}))
    db.add(review)
    db.flush()
    record_activity(db, action="review.created", subject_type=body.subject_type, subject_id=body.subject_id, profile_id=actor.id, details={"review_id": review.id, "rating": body.rating, "decision": body.decision})
    db.commit()
    return attributed(db, review)


@router.get("/reviews")
def reviews(db: DB, subject_type: str | None = None, subject_id: str | None = None, subject: str | None = None, project_id: str | None = None, decision: str | None = None, profile_id: str | None = None):
    query = select(models.Review).order_by(models.Review.created_at.desc())
    if subject and ":" in subject:
        subject_type, subject_id = subject.split(":", 1)
    if subject_type:
        query = query.where(models.Review.subject_type == subject_type)
    if subject_id:
        query = query.where(models.Review.subject_id == subject_id)
    if decision:
        query = query.where(models.Review.decision == decision)
    if profile_id:
        query = query.where(models.Review.profile_id == profile_id)
    # Polymorphic subjects cannot be joined generically; project filtering covers subjects with direct activity attribution.
    if project_id:
        subject_ids = select(models.ActivityEvent.subject_id).where(models.ActivityEvent.project_id == project_id)
        query = query.where(models.Review.subject_id.in_(subject_ids))
    return [attributed(db, row) for row in db.scalars(query).all()]


@router.post("/comments", status_code=status.HTTP_201_CREATED)
def create_comment(body: schemas.CommentCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    actor = active_profile(db, body.profile_id or x_profile_id)
    comment = models.Comment(workspace_id=current_workspace(db).id, profile_id=actor.id, **body.model_dump(exclude={"profile_id"}))
    db.add(comment)
    db.flush()
    record_activity(db, action="comment.created", subject_type=body.subject_type, subject_id=body.subject_id, profile_id=actor.id, details={"comment_id": comment.id})
    db.commit()
    return attributed(db, comment)


@router.get("/comments")
def comments(db: DB, subject_type: str | None = None, subject_id: str | None = None, subject: str | None = None):
    if subject and ":" in subject:
        subject_type, subject_id = subject.split(":", 1)
    if not subject_type or not subject_id:
        raise HTTPException(status_code=422, detail="subject_type and subject_id, or subject, are required")
    query = select(models.Comment).where(models.Comment.subject_type == subject_type, models.Comment.subject_id == subject_id).order_by(models.Comment.created_at)
    return [attributed(db, row) for row in db.scalars(query).all()]


@router.post("/notes", status_code=status.HTTP_201_CREATED)
def create_note(body: schemas.NoteCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    actor = active_profile(db, body.profile_id or x_profile_id)
    if body.supersedes_id:
        previous = get_or_404(db, models.Note, body.supersedes_id)
        if previous.subject_type != body.subject_type or previous.subject_id != body.subject_id:
            raise HTTPException(status_code=400, detail="superseded note has another subject")
        revision = previous.revision + 1
    else:
        revision = 1
    note = models.Note(workspace_id=current_workspace(db).id, profile_id=actor.id, revision=revision, **body.model_dump(exclude={"profile_id"}))
    db.add(note)
    db.flush()
    record_activity(db, action="note.created", subject_type=body.subject_type, subject_id=body.subject_id, profile_id=actor.id, details={"note_id": note.id, "revision": revision})
    db.commit()
    return attributed(db, note)


@router.get("/notes")
def notes(db: DB, subject_type: str, subject_id: str):
    return [attributed(db, row) for row in db.scalars(select(models.Note).where(models.Note.subject_type == subject_type, models.Note.subject_id == subject_id).order_by(models.Note.created_at.desc())).all()]


@router.post("/lineage", status_code=status.HTTP_201_CREATED)
def create_lineage(body: schemas.LineageCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    edge = models.LineageEdge(workspace_id=current_workspace(db).id, metadata_=body.metadata, **body.model_dump(exclude={"metadata"}))
    db.add(edge)
    db.flush()
    record_activity(db, action="lineage.created", subject_type=body.target_type, subject_id=body.target_id, profile_id=active_profile(db, x_profile_id).id, details={"edge_id": edge.id, "relationship": edge.relationship})
    db.commit()
    return dump(edge)


@router.get("/lineage")
def lineage(db: DB, subject_type: str, subject_id: str):
    query = select(models.LineageEdge).where(or_(
        (models.LineageEdge.source_type == subject_type) & (models.LineageEdge.source_id == subject_id),
        (models.LineageEdge.target_type == subject_type) & (models.LineageEdge.target_id == subject_id),
    )).order_by(models.LineageEdge.created_at)
    return [{**dump(row), "preview_asset_id": _lineage_preview(db, row)} for row in db.scalars(query).all()]


def _activity_enrichment(db: Session, rows: list[models.ActivityEvent]) -> dict[tuple[str, str], dict[str, str | None]]:
    """Resolve activity subjects in batches, keeping unknown subjects entirely null."""
    aliases = {"training_run": "run", "import_job": "import_job"}
    ids_by_type: dict[str, set[str]] = defaultdict(set)
    fal_targets: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for row in rows:
        if row.subject_type != "fal_admission":
            continue
        details = row.details if isinstance(row.details, dict) else {}
        eval_run_id = details.get("eval_run_id")
        job_id = details.get("job_id")
        eval_run_id = str(eval_run_id) if eval_run_id else None
        job_id = str(job_id) if job_id else None
        fal_targets[("fal_admission", row.subject_id)] = (eval_run_id, job_id)
        if eval_run_id:
            ids_by_type["eval_run"].add(eval_run_id)
        if job_id:
            ids_by_type["job"].add(job_id)
    for row in rows:
        subject_type = aliases.get(row.subject_type, row.subject_type)
        if subject_type in {"project", "dataset", "asset", "run", "checkpoint", "eval_run", "grid_definition", "job", "import_job", "model"}:
            ids_by_type[subject_type].add(row.subject_id)

    projects = {
        item.id: item
        for item in db.scalars(select(models.Project).where(models.Project.id.in_(ids_by_type["project"]))).all()
    } if ids_by_type["project"] else {}
    datasets = {
        item.id: item
        for item in db.scalars(select(models.Dataset).where(models.Dataset.id.in_(ids_by_type["dataset"]))).all()
    } if ids_by_type["dataset"] else {}
    assets = {
        item.id: item
        for item in db.scalars(select(models.Asset).where(models.Asset.id.in_(ids_by_type["asset"]))).all()
    } if ids_by_type["asset"] else {}
    runs = {
        item.id: item
        for item in db.scalars(select(models.TrainingRun).where(models.TrainingRun.id.in_(ids_by_type["run"]))).all()
    } if ids_by_type["run"] else {}
    checkpoints = {
        item.id: item
        for item in db.scalars(select(models.Checkpoint).where(models.Checkpoint.id.in_(ids_by_type["checkpoint"]))).all()
    } if ids_by_type["checkpoint"] else {}
    eval_runs = {
        item.id: item
        for item in db.scalars(select(models.EvalRun).where(models.EvalRun.id.in_(ids_by_type["eval_run"]))).all()
    } if ids_by_type["eval_run"] else {}
    grids = {
        item.id: item
        for item in db.scalars(select(models.GridDefinition).where(models.GridDefinition.id.in_(ids_by_type["grid_definition"]))).all()
    } if ids_by_type["grid_definition"] else {}
    jobs = {
        item.id: item
        for item in db.scalars(select(models.Job).where(models.Job.id.in_(ids_by_type["job"]))).all()
    } if ids_by_type["job"] else {}

    def job_preview_ids(value: Any) -> list[str]:
        if isinstance(value, dict):
            found: list[str] = []
            for field in ("asset_id", "preview_asset_id"):
                candidate = value.get(field)
                if candidate:
                    found.append(str(candidate))
            candidates = value.get("asset_ids")
            if isinstance(candidates, str):
                found.append(candidates)
            elif isinstance(candidates, (list, tuple)):
                found.extend(str(candidate) for candidate in candidates if candidate)
            for field in ("outputs", "result", "payload"):
                found.extend(job_preview_ids(value.get(field)))
            return found
        if isinstance(value, (list, tuple)):
            return [candidate for item in value for candidate in job_preview_ids(item)]
        return []

    job_preview_candidates: dict[str, list[str]] = {}
    job_asset_ids: set[str] = set()
    for job_id in {job_id for _, job_id in fal_targets.values() if job_id}:
        item = jobs.get(job_id)
        if item is None:
            continue
        candidates = job_preview_ids(item.payload) + job_preview_ids(item.result)
        if candidates:
            job_preview_candidates[job_id] = candidates
            job_asset_ids.update(candidates)
    job_preview_assets = {
        item.id: item
        for item in db.scalars(
            select(models.Asset).where(
                models.Asset.id.in_(job_asset_ids),
                models.Asset.kind == models.AssetKind.image,
            )
        ).all()
    } if job_asset_ids else {}
    import_jobs = {
        item.id: item
        for item in db.scalars(select(models.ImportJob).where(models.ImportJob.id.in_(ids_by_type["import_job"]))).all()
    } if ids_by_type["import_job"] else {}
    models_by_id = {
        item.id: item
        for item in db.scalars(select(models.Model).where(models.Model.id.in_(ids_by_type["model"]))).all()
    } if ids_by_type["model"] else {}
    eval_definitions = {}
    if eval_runs:
        definition_ids = {item.definition_id for item in eval_runs.values()}
        eval_definitions = {
            item.id: item
            for item in db.scalars(select(models.EvalDefinition).where(models.EvalDefinition.id.in_(definition_ids))).all()
        }

    previews: dict[tuple[str, str], str] = {}

    def assign(key: tuple[str, str], asset_id: str | None):
        if asset_id and key not in previews:
            previews[key] = asset_id

    if assets:
        for asset_id, asset in assets.items():
            if asset.kind == models.AssetKind.image:
                assign(("asset", asset_id), asset_id)

    if projects:
        project_assets = db.execute(
            select(models.Asset.project_id, models.Asset.id)
            .where(models.Asset.project_id.in_(projects), models.Asset.kind == models.AssetKind.image)
            .order_by(models.Asset.updated_at.desc(), models.Asset.created_at.desc(), models.Asset.id.desc())
        ).all()
        for project_id, asset_id in project_assets:
            assign(("project", project_id), asset_id)
        project_eval_assets = db.execute(
            select(models.EvalDefinition.project_id, models.Asset.id)
            .join(models.EvalRun, models.EvalRun.definition_id == models.EvalDefinition.id)
            .join(models.EvalOutput, models.EvalOutput.eval_run_id == models.EvalRun.id)
            .join(models.Asset, models.Asset.id == models.EvalOutput.asset_id)
            .where(models.EvalDefinition.project_id.in_(projects), models.Asset.kind == models.AssetKind.image)
            .order_by(models.EvalOutput.generated_at.desc().nulls_last(), models.EvalOutput.created_at.desc(), models.EvalOutput.id.desc())
        ).all()
        for project_id, asset_id in project_eval_assets:
            assign(("project", project_id), asset_id)

    if datasets:
        dataset_assets = db.execute(
            select(models.DatasetVersion.dataset_id, models.Asset.id)
            .join(models.DatasetItem, models.DatasetItem.dataset_version_id == models.DatasetVersion.id)
            .join(models.Asset, models.Asset.id == models.DatasetItem.asset_id)
            .where(
                models.DatasetVersion.dataset_id.in_(datasets),
                models.DatasetItem.included.is_(True),
                models.Asset.kind == models.AssetKind.image,
            )
            .order_by(models.DatasetVersion.version_number.desc(), models.DatasetItem.position, models.Asset.id)
        ).all()
        for dataset_id, asset_id in dataset_assets:
            assign(("dataset", dataset_id), asset_id)

    run_ids = set(runs)
    checkpoint_ids = set(checkpoints)
    sample_rows = []
    if run_ids or checkpoint_ids:
        sample_query = (
            select(models.Sample.run_id, models.Sample.checkpoint_id, models.Sample.asset_id)
            .join(models.Asset, models.Asset.id == models.Sample.asset_id)
            .where(models.Asset.kind == models.AssetKind.image)
            .order_by(
                models.Sample.step.desc().nulls_last(),
                models.Sample.modified_at.desc().nulls_last(),
                models.Sample.created_at.desc(),
                models.Sample.id.desc(),
            )
        )
        if run_ids and checkpoint_ids:
            sample_query = sample_query.where(or_(models.Sample.run_id.in_(run_ids), models.Sample.checkpoint_id.in_(checkpoint_ids)))
        elif run_ids:
            sample_query = sample_query.where(models.Sample.run_id.in_(run_ids))
        else:
            sample_query = sample_query.where(models.Sample.checkpoint_id.in_(checkpoint_ids))
        sample_rows = db.execute(sample_query).all()
    for run_id, checkpoint_id, asset_id in sample_rows:
        if run_id in run_ids:
            assign(("run", run_id), asset_id)
        if checkpoint_id in checkpoint_ids:
            assign(("checkpoint", checkpoint_id), asset_id)

    if runs:
        checkpoint_assets = db.execute(
            select(models.Checkpoint.run_id, models.Asset.id)
            .join(models.Asset, models.Asset.id == models.Checkpoint.asset_id)
            .where(models.Checkpoint.run_id.in_(runs), models.Asset.kind == models.AssetKind.image)
            .order_by(models.Checkpoint.step.desc(), models.Checkpoint.created_at.desc(), models.Checkpoint.id.desc())
        ).all()
        for run_id, asset_id in checkpoint_assets:
            assign(("run", run_id), asset_id)

    if checkpoints:
        checkpoint_assets = db.execute(
            select(models.Checkpoint.id, models.Asset.id)
            .join(models.Asset, models.Asset.id == models.Checkpoint.asset_id)
            .where(models.Checkpoint.id.in_(checkpoints), models.Asset.kind == models.AssetKind.image)
        ).all()
        for checkpoint_id, asset_id in checkpoint_assets:
            assign(("checkpoint", checkpoint_id), asset_id)

    if eval_runs:
        eval_assets = db.execute(
            select(models.EvalOutput.eval_run_id, models.Asset.id)
            .join(models.Asset, models.Asset.id == models.EvalOutput.asset_id)
            .where(models.EvalOutput.eval_run_id.in_(eval_runs), models.Asset.kind == models.AssetKind.image)
            .order_by(models.EvalOutput.generated_at.desc().nulls_last(), models.EvalOutput.created_at.desc(), models.EvalOutput.id.desc())
        ).all()
        for eval_run_id, asset_id in eval_assets:
            assign(("eval_run", eval_run_id), asset_id)

    if grids:
        grid_assets = db.execute(
            select(models.GridCell.grid_definition_id, models.Asset.id)
            .join(models.EvalOutput, models.EvalOutput.id == models.GridCell.eval_output_id)
            .join(models.Asset, models.Asset.id == models.EvalOutput.asset_id)
            .where(models.GridCell.grid_definition_id.in_(grids), models.Asset.kind == models.AssetKind.image)
            .order_by(models.GridCell.y_index, models.GridCell.x_index, models.GridCell.id)
        ).all()
        for grid_id, asset_id in grid_assets:
            assign(("grid_definition", grid_id), asset_id)

    if models_by_id:
        model_assets = db.execute(
            select(models.ModelVersion.model_id, models.Asset.id)
            .join(models.Checkpoint, models.Checkpoint.id == models.ModelVersion.checkpoint_id)
            .join(models.Sample, models.Sample.checkpoint_id == models.Checkpoint.id)
            .join(models.Asset, models.Asset.id == models.Sample.asset_id)
            .where(models.ModelVersion.model_id.in_(models_by_id), models.Asset.kind == models.AssetKind.image)
            .order_by(
                models.Sample.step.desc().nulls_last(),
                models.Sample.modified_at.desc().nulls_last(),
                models.Sample.created_at.desc(),
                models.Sample.id.desc(),
            )
        ).all()
        for model_id, asset_id in model_assets:
            assign(("model", model_id), asset_id)
        model_eval_assets = db.execute(
            select(models.ModelVersion.model_id, models.Asset.id)
            .join(models.EvalDefinition, models.EvalDefinition.model_version_id == models.ModelVersion.id)
            .join(models.EvalRun, models.EvalRun.definition_id == models.EvalDefinition.id)
            .join(models.EvalOutput, models.EvalOutput.eval_run_id == models.EvalRun.id)
            .join(models.Asset, models.Asset.id == models.EvalOutput.asset_id)
            .where(models.ModelVersion.model_id.in_(models_by_id), models.Asset.kind == models.AssetKind.image)
            .order_by(models.EvalOutput.generated_at.desc().nulls_last(), models.EvalOutput.created_at.desc(), models.EvalOutput.id.desc())
        ).all()
        for model_id, asset_id in model_eval_assets:
            assign(("model", model_id), asset_id)
        model_checkpoint_assets = db.execute(
            select(models.ModelVersion.model_id, models.Asset.id)
            .join(models.Checkpoint, models.Checkpoint.id == models.ModelVersion.checkpoint_id)
            .join(models.Asset, models.Asset.id == models.Checkpoint.asset_id)
            .where(models.ModelVersion.model_id.in_(models_by_id), models.Asset.kind == models.AssetKind.image)
            .order_by(models.Checkpoint.step.desc(), models.Checkpoint.created_at.desc(), models.Checkpoint.id.desc())
        ).all()
        for model_id, asset_id in model_checkpoint_assets:
            assign(("model", model_id), asset_id)

    result: dict[tuple[str, str], dict[str, str | None]] = {}
    for subject_type, subject_ids in ids_by_type.items():
        for subject_id in subject_ids:
            item = (
                projects.get(subject_id) if subject_type == "project"
                else datasets.get(subject_id) if subject_type == "dataset"
                else assets.get(subject_id) if subject_type == "asset" and assets.get(subject_id, None) and assets[subject_id].kind == models.AssetKind.image
                else runs.get(subject_id) if subject_type == "run"
                else checkpoints.get(subject_id) if subject_type == "checkpoint"
                else eval_runs.get(subject_id) if subject_type == "eval_run"
                else grids.get(subject_id) if subject_type == "grid_definition"
                else jobs.get(subject_id) if subject_type == "job"
                else import_jobs.get(subject_id) if subject_type == "import_job"
                else models_by_id.get(subject_id) if subject_type == "model"
                else None
            )
            if item is None:
                continue
            if subject_type == "project":
                href, label = f"#/project/{item.id}", item.title
            elif subject_type == "dataset":
                href, label = f"#/dataset/{item.id}", item.name
            elif subject_type == "asset":
                href, label = f"#/image/{item.id}", item.name
            elif subject_type == "run":
                href, label = f"#/run/{item.id}", item.name
            elif subject_type == "checkpoint":
                href, label = f"#/checkpoint/{item.id}", f"step {item.step}"
            elif subject_type == "eval_run":
                definition = eval_definitions.get(item.definition_id)
                href, label = f"#/eval/{item.id}", definition.name if definition else f"Eval run · {item.status}"
            elif subject_type == "grid_definition":
                href, label = f"#/grid/{item.id}", item.name
            elif subject_type == "job":
                href, label = f"#/jobs/{item.id}?job=1", f"Transfer job · {item.kind}"
            elif subject_type == "import_job":
                descriptor = item.detected_type or item.prefix
                href, label = f"#/jobs/{item.id}", f"Import job · {descriptor}"
            else:
                href, label = f"#/model/{item.id}", item.name
            result[(subject_type, subject_id)] = {
                "object_href": href,
                "object_label": label,
                "preview_asset_id": previews.get((subject_type, subject_id)),
            }
    for key, (eval_run_id, job_id) in fal_targets.items():
        run = eval_runs.get(eval_run_id) if eval_run_id else None
        if run is not None:
            definition = eval_definitions.get(run.definition_id)
            result[key] = {
                "object_href": f"#/eval/{run.id}",
                "object_label": definition.name if definition and definition.name else f"Eval run · {run.status}",
                "preview_asset_id": previews.get(("eval_run", run.id)),
            }
            continue
        job = jobs.get(job_id) if job_id else None
        if job is not None:
            preview_asset_id = next(
                (asset_id for asset_id in job_preview_candidates.get(job.id, []) if asset_id in job_preview_assets),
                None,
            )
            result[key] = {
                "object_href": f"#/jobs/{job.id}?job=1",
                "object_label": f"Transfer job · {job.kind}",
                "preview_asset_id": preview_asset_id,
            }
    return result


@router.get("/activity")
def activity(db: DB, project_id: str | None = None, subject_type: str | None = None, subject_id: str | None = None, profile_id: str | None = None, since: str | None = None, limit: int = Query(100, le=500)):
    query = select(models.ActivityEvent).order_by(models.ActivityEvent.created_at.desc())
    if project_id:
        query = query.where(models.ActivityEvent.project_id == project_id)
    if subject_type:
        query = query.where(models.ActivityEvent.subject_type == subject_type)
    if subject_id:
        query = query.where(models.ActivityEvent.subject_id == subject_id)
    if profile_id:
        query = query.where(models.ActivityEvent.profile_id == profile_id)
    if since:
        from datetime import datetime
        try:
            since_value = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="since must be an ISO-8601 datetime") from exc
        query = query.where(models.ActivityEvent.created_at >= since_value)
    rows = db.scalars(query.limit(limit)).all()
    profile_ids = {row.profile_id for row in rows if row.profile_id}
    names = {
        profile.id: profile.display_name
        for profile in db.scalars(select(models.UserProfile).where(models.UserProfile.id.in_(profile_ids))).all()
    } if profile_ids else {}
    enrichment = _activity_enrichment(db, rows)
    result = []
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        result.append({
            **dump(row),
            "event_type": row.action,
            "summary": details.get("summary") or row.action.replace(".", " ").replace("_", " ").title(),
            "profile_name": names.get(row.profile_id),
            **enrichment.get(({"training_run": "run", "import_job": "import_job"}.get(row.subject_type, row.subject_type), row.subject_id), {
                "object_href": None,
                "object_label": None,
                "preview_asset_id": None,
            }),
        })
    return result


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def create_job(body: schemas.JobCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    actor = active_profile(db, body.profile_id or x_profile_id)
    job = models.Job(workspace_id=current_workspace(db).id, profile_id=actor.id, **body.model_dump(exclude={"profile_id"}))
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if body.idempotency_key:
            existing = db.scalar(select(models.Job).where(models.Job.idempotency_key == body.idempotency_key))
            if existing:
                return dump(existing)
        raise
    record_activity(db, action="job.queued", subject_type="job", subject_id=job.id, profile_id=actor.id, details={"kind": job.kind})
    db.commit()
    return dump(job)


@router.get("/jobs")
def jobs(db: DB, state: str | None = None, kind: str | None = None):
    query = select(models.Job).order_by(models.Job.created_at.desc())
    if state:
        query = query.where(models.Job.state == state)
    if kind:
        query = query.where(models.Job.kind == kind)
    return [dump(row) for row in db.scalars(query).all()]


@router.get("/jobs/{job_id}")
def job(job_id: str, db: DB):
    return dump(get_or_404(db, models.Job, job_id))


@router.get("/jobs/{job_id}/logs")
def job_logs(job_id: str, db: DB, tail: int = Query(100, ge=1, le=1000)):
    job = get_or_404(db, models.Job, job_id)
    events = db.scalars(select(models.ActivityEvent).where(models.ActivityEvent.subject_type == "job", models.ActivityEvent.subject_id == job.id).order_by(models.ActivityEvent.created_at.desc()).limit(tail)).all()
    rows = [{"timestamp": event.created_at, "level": "info", "event": event.action, "details": event.details} for event in reversed(events)]
    if job.error:
        rows.append({"timestamp": job.updated_at, "level": "error", "event": "job.error", "message": job.error})
    if job.result:
        rows.append({"timestamp": job.updated_at, "level": "info", "event": "job.result", "details": job.result})
    return rows[-tail:]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    job = get_or_404(db, models.Job, job_id)
    if job.state in (models.JobState.succeeded, models.JobState.failed, models.JobState.canceled):
        raise HTTPException(status_code=409, detail="job is already terminal")
    job.cancel_requested = True
    record_activity(db, action="job.cancel_requested", subject_type="job", subject_id=job.id, profile_id=active_profile(db, x_profile_id).id)
    db.commit()
    return dump(job)


@router.post("/prompt-sets", status_code=status.HTTP_201_CREATED)
def create_prompt_set(body: schemas.PromptSetCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    get_or_404(db, models.Project, body.project_id)
    prompt_set = models.PromptSet(project_id=body.project_id, name=body.name)
    db.add(prompt_set)
    db.flush()
    for index, value in enumerate(body.prompts):
        if isinstance(value, dict):
            text = value.get("prompt") or value.get("text")
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(status_code=422, detail=f"prompt at index {index} requires prompt or text")
            metadata = {key: item for key, item in value.items() if key not in {"prompt", "text"}}
        else:
            text, metadata = value, {}
        db.add(models.Prompt(prompt_set_id=prompt_set.id, text=text, position=index, metadata_=metadata))
    record_activity(db, action="prompt_set.created", subject_type="prompt_set", subject_id=prompt_set.id, profile_id=active_profile(db, x_profile_id).id, project_id=body.project_id, details={"prompts": len(body.prompts)})
    db.commit()
    return dump(prompt_set)


@router.get("/prompt-sets")
def prompt_sets(db: DB, project_id: str | None = None):
    query = select(models.PromptSet).order_by(models.PromptSet.updated_at.desc())
    if project_id:
        query = query.where(models.PromptSet.project_id == project_id)
    rows = []
    for row in db.scalars(query).all():
        project = db.get(models.Project, row.project_id)
        rows.append({
            **dump(row),
            "project_name": project.title if project else None,
            "prompt_count": int(db.scalar(select(func.count()).select_from(models.Prompt).where(models.Prompt.prompt_set_id == row.id)) or 0),
        })
    return rows


@router.get("/prompt-sets/{prompt_set_id}")
def prompt_set_detail(prompt_set_id: str, db: DB):
    prompt_set = get_or_404(db, models.PromptSet, prompt_set_id)
    prompts = db.scalars(select(models.Prompt).where(models.Prompt.prompt_set_id == prompt_set.id).order_by(models.Prompt.position)).all()
    return {**dump(prompt_set), "prompts": [dump(prompt) for prompt in prompts]}


@router.put("/prompt-sets/{prompt_set_id}")
def update_prompt_set(prompt_set_id: str, body: schemas.PromptSetUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    previous = get_or_404(db, models.PromptSet, prompt_set_id)
    new = models.PromptSet(project_id=previous.project_id, name=body.name or previous.name, version=previous.version + 1)
    db.add(new)
    db.flush()
    texts = body.prompts
    if texts is None:
        texts = list(db.scalars(select(models.Prompt.text).where(models.Prompt.prompt_set_id == previous.id).order_by(models.Prompt.position)))
    for position, value in enumerate(texts):
        if isinstance(value, dict):
            text = value.get("prompt") or value.get("text")
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(status_code=422, detail=f"prompt at index {position} requires prompt or text")
            metadata = {key: item for key, item in value.items() if key not in {"prompt", "text"}}
        else:
            text, metadata = value, {}
        db.add(models.Prompt(prompt_set_id=new.id, text=text, position=position, metadata_=metadata))
    record_activity(db, action="prompt_set.versioned", subject_type="prompt_set", subject_id=new.id, profile_id=active_profile(db, x_profile_id).id, project_id=new.project_id, details={"previous_id": previous.id, "version": new.version})
    db.commit()
    return prompt_set_detail(new.id, db)


@router.post("/prompt-sets/{prompt_set_id}/versions", status_code=status.HTTP_201_CREATED)
def version_prompt_set(prompt_set_id: str, body: schemas.PromptSetUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    return update_prompt_set(prompt_set_id, body, db, x_profile_id)


@router.post("/eval-definitions", status_code=status.HTTP_201_CREATED)
def create_eval_definition(body: schemas.EvalDefinitionCreate, db: DB):
    try:
        from ..integrations.fal.adapters import get_adapter
        get_adapter(body.endpoint)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    get_or_404(db, models.Project, body.project_id)
    if body.prompt_set_id:
        prompt_set = get_or_404(db, models.PromptSet, body.prompt_set_id)
        if prompt_set.project_id != body.project_id:
            raise HTTPException(status_code=409, detail="prompt set belongs to another project")
    if body.model_version_id:
        version = get_or_404(db, models.ModelVersion, body.model_version_id)
        model = get_or_404(db, models.Model, version.model_id)
        if model.project_id != body.project_id:
            raise HTTPException(status_code=409, detail="model version belongs to another project")
    definition = models.EvalDefinition(**body.model_dump())
    db.add(definition)
    db.commit()
    return dump(definition)


@router.get("/eval-definitions")
def eval_definitions(db: DB, project_id: str | None = None):
    query = select(models.EvalDefinition).order_by(models.EvalDefinition.updated_at.desc())
    if project_id:
        query = query.where(models.EvalDefinition.project_id == project_id)
    return [dump(row) for row in db.scalars(query).all()]


@router.get("/eval-definitions/{definition_id}")
def eval_definition(definition_id: str, db: DB):
    return dump(get_or_404(db, models.EvalDefinition, definition_id))


@router.post("/eval-runs", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
def create_eval_run(body: schemas.EvalRunCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    if not body.dry_run:
        raise HTTPException(status_code=409, detail={"code": "FAL_ADMISSION_REQUIRED", "message": "Create and admit a FAL admission before provider work."})
    if body.definition_id:
        definition = get_or_404(db, models.EvalDefinition, body.definition_id)
    else:
        endpoint = body.endpoint or body.endpoint_adapter
        try:
            from ..integrations.fal.adapters import get_adapter
            get_adapter(str(endpoint))
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        version = get_or_404(db, models.ModelVersion, str(body.model_version_id))
        model = get_or_404(db, models.Model, version.model_id)
        prompt_set = get_or_404(db, models.PromptSet, str(body.prompt_set_id))
        if prompt_set.project_id != model.project_id:
            raise HTTPException(status_code=400, detail="model version and prompt set belong to different projects")
        definition = db.scalar(select(models.EvalDefinition).where(
            models.EvalDefinition.project_id == model.project_id,
            models.EvalDefinition.endpoint == endpoint,
            models.EvalDefinition.model_version_id == version.id,
            models.EvalDefinition.prompt_set_id == prompt_set.id,
            models.EvalDefinition.parameters == body.parameters,
        ))
        if definition is None:
            definition = models.EvalDefinition(project_id=model.project_id, name=f"{model.name} / {prompt_set.name}", endpoint=str(endpoint), model_version_id=version.id, prompt_set_id=prompt_set.id, parameters=body.parameters)
            db.add(definition)
            db.flush()
    checkpoint_id = body.checkpoint_id
    if checkpoint_id:
        get_or_404(db, models.Checkpoint, checkpoint_id)
    elif definition.model_version_id:
        checkpoint_id = get_or_404(db, models.ModelVersion, definition.model_version_id).checkpoint_id
    actor = active_profile(db, x_profile_id)
    run = models.EvalRun(definition_id=definition.id, checkpoint_id=checkpoint_id, status="preview" if body.dry_run else "queued", parameters_snapshot={**definition.parameters, **body.parameters})
    db.add(run)
    db.flush()
    if body.dry_run:
        db.commit()
        return {**dump(run), "definition_id": definition.id, "dry_run": True, "job_id": None}
    job = models.Job(workspace_id=current_workspace(db).id, kind="fal.eval_batch", profile_id=actor.id, payload={"eval_run_id": run.id})
    db.add(job)
    record_activity(db, action="eval.queued", subject_type="eval_run", subject_id=run.id, profile_id=actor.id, project_id=definition.project_id, details={"job_id": job.id})
    db.commit()
    return {**dump(run), "job_id": job.id}


@router.get("/eval-runs/{run_id}")
def eval_run(run_id: str, db: DB):
    run = get_or_404(db, models.EvalRun, run_id)
    definition = get_or_404(db, models.EvalDefinition, run.definition_id)
    outputs = db.scalars(select(models.EvalOutput).where(models.EvalOutput.eval_run_id == run.id).order_by(models.EvalOutput.created_at)).all()
    jobs = db.scalars(select(models.Job).where(models.Job.payload["eval_run_id"].as_string() == run.id).order_by(models.Job.created_at.desc())).all()
    latest_job = jobs[0] if jobs else None
    output_rows = []
    for output in outputs:
        inline_id = str((output.provider_metadata or {}).get("inline_prompt_id") or output.prompt_id or "")
        inline = next((item for item in (definition.inline_prompts or []) if str(item.get("id")) == inline_id), None)
        prompt = db.get(models.Prompt, output.prompt_id) if output.prompt_id else None
        asset = db.get(models.Asset, output.asset_id)
        image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == output.asset_id))
        output_rows.append(
            {
                **dump(output),
                "asset_revision_id": output.asset_id,
                "prompt_id": inline_id or output.prompt_id,
                "prompt": prompt.text if prompt else (inline or {}).get("text"),
                "filename": asset.name if asset else None,
                "mime_type": asset.mime_type if asset else None,
                "asset_metadata": dict(asset.metadata_ or {}) if asset else {},
                "width": image.width if image else None,
                "height": image.height if image else None,
            }
        )
    version = db.get(models.ModelVersion, definition.model_version_id) if definition.model_version_id else None
    model = db.get(models.Model, version.model_id) if version else None
    return {**dump(run), "name": definition.name, "project_id": definition.project_id, "endpoint_adapter": definition.endpoint,
        "model_version_id": definition.model_version_id, "model_version_name": version.name if version else None,
        "model_name": model.name if model else None, "prompt_set_id": definition.prompt_set_id,
        "inline_prompts": definition.inline_prompts, "prompt_count": len(definition.inline_prompts or []),
        "parameters": run.parameters_snapshot, "progress": latest_job.progress if latest_job else (1 if run.status == "succeeded" else 0), "job_id": latest_job.id if latest_job else None, "outputs": output_rows}


@router.get("/eval-runs")
def eval_runs(db: DB, definition_id: str | None = None, project_id: str | None = None, run_status: str | None = Query(None, alias="status")):
    query = select(models.EvalRun).order_by(models.EvalRun.created_at.desc())
    if definition_id:
        query = query.where(models.EvalRun.definition_id == definition_id)
    if run_status:
        query = query.where(models.EvalRun.status == run_status)
    if project_id:
        query = query.join(models.EvalDefinition, models.EvalDefinition.id == models.EvalRun.definition_id).where(models.EvalDefinition.project_id == project_id)
    rows = []
    for row in db.scalars(query):
        definition = db.get(models.EvalDefinition, row.definition_id)
        version = db.get(models.ModelVersion, definition.model_version_id) if definition and definition.model_version_id else None
        prompt_set = db.get(models.PromptSet, definition.prompt_set_id) if definition else None
        job = db.scalar(select(models.Job).where(models.Job.payload["eval_run_id"].as_string() == row.id).order_by(models.Job.created_at.desc()))
        outputs = list(db.scalars(select(models.EvalOutput).where(models.EvalOutput.eval_run_id == row.id).order_by(models.EvalOutput.created_at)))
        rows.append({**dump(row), "name": definition.name if definition else None, "endpoint_adapter": definition.endpoint if definition else None, "model_version_id": definition.model_version_id if definition else None, "model_version_name": version.name if version else None, "prompt_set_id": definition.prompt_set_id if definition else None, "prompt_set_name": prompt_set.name if prompt_set else None,
            "prompt_count": len(definition.inline_prompts or []) if definition else 0, "output_count": len(outputs),
            "outputs": [{"asset_id": output.asset_id, "asset_revision_id": output.asset_id} for output in outputs[:4]],
            "parameters": row.parameters_snapshot, "progress": job.progress if job else (1 if row.status == "succeeded" else 0), "job_id": job.id if job else None})
    return rows


@router.get("/eval-runs/{run_id}/outputs")
def eval_outputs(run_id: str, db: DB):
    run = get_or_404(db, models.EvalRun, run_id)
    definition = get_or_404(db, models.EvalDefinition, run.definition_id)
    rows = []
    for output in db.scalars(select(models.EvalOutput).where(models.EvalOutput.eval_run_id == run_id).order_by(models.EvalOutput.created_at)).all():
        inline_id = str((output.provider_metadata or {}).get("inline_prompt_id") or output.prompt_id or "")
        inline = next((item for item in (definition.inline_prompts or []) if str(item.get("id")) == inline_id), None)
        prompt = db.get(models.Prompt, output.prompt_id) if output.prompt_id else None
        asset = db.get(models.Asset, output.asset_id)
        image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == output.asset_id))
        rows.append(
            {
                **dump(output),
                "asset_revision_id": output.asset_id,
                "prompt_id": inline_id or output.prompt_id,
                "prompt": prompt.text if prompt else (inline or {}).get("text"),
                "filename": asset.name if asset else None,
                "mime_type": asset.mime_type if asset else None,
                "asset_metadata": dict(asset.metadata_ or {}) if asset else {},
                "width": image.width if image else None,
                "height": image.height if image else None,
            }
        )
    return rows


@router.post("/eval-runs/{run_id}/cancel")
def cancel_eval_run(run_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    run = get_or_404(db, models.EvalRun, run_id)
    if run.status in {"succeeded", "failed", "canceled"}:
        raise HTTPException(status_code=409, detail="eval run is already terminal")
    run.status = "canceled"
    jobs = db.scalars(select(models.Job).where(models.Job.payload["eval_run_id"].as_string() == run.id)).all()
    for job in jobs:
        job.cancel_requested = True
    definition = get_or_404(db, models.EvalDefinition, run.definition_id)
    record_activity(db, action="eval.canceled", subject_type="eval_run", subject_id=run.id, profile_id=active_profile(db, x_profile_id).id, project_id=definition.project_id)
    db.commit()
    return dump(run)


@router.post("/grid-definitions", status_code=status.HTTP_201_CREATED)
def create_grid_definition(body: schemas.GridDefinitionCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    definition = get_or_404(db, models.EvalDefinition, body.eval_definition_id)
    if definition.project_id != body.project_id:
        raise HTTPException(status_code=400, detail="eval definition belongs to another project")
    grid = models.GridDefinition(**body.model_dump())
    db.add(grid)
    db.flush()
    record_activity(db, action="grid.created", subject_type="grid_definition", subject_id=grid.id, profile_id=active_profile(db, x_profile_id).id, project_id=grid.project_id)
    db.commit()
    return dump(grid)


@router.get("/grid-definitions")
def grid_definitions(db: DB, project_id: str | None = None):
    query = select(models.GridDefinition).order_by(models.GridDefinition.created_at.desc())
    if project_id:
        query = query.where(models.GridDefinition.project_id == project_id)
    return [dump(row) for row in db.scalars(query).all()]


@router.get("/grid-definitions/{grid_id}")
def grid_definition(grid_id: str, db: DB):
    grid = get_or_404(db, models.GridDefinition, grid_id)
    cells = db.scalars(select(models.GridCell).where(models.GridCell.grid_definition_id == grid.id).order_by(models.GridCell.y_index, models.GridCell.x_index)).all()
    jobs = db.scalars(select(models.Job).where(models.Job.payload["grid_definition_id"].as_string() == grid.id).order_by(models.Job.created_at.desc())).all()
    x_values, y_values = grid.x_axis.get("values", []), grid.y_axis.get("values", [])
    cell_rows = []
    for cell in cells:
        output = db.get(models.EvalOutput, cell.eval_output_id)
        cell_rows.append({
            **dump(cell),
            "asset_id": output.asset_id if output else None,
            "asset_revision_id": output.asset_id if output else None,
            "seed": output.seed if output else None,
            "x_value": x_values[cell.x_index] if cell.x_index < len(x_values) else None,
            "y_value": y_values[cell.y_index] if cell.y_index < len(y_values) else None,
        })
    definition = db.get(models.EvalDefinition, grid.eval_definition_id)
    return {**dump(grid), "endpoint_adapter": definition.endpoint if definition else None, "model_version_id": definition.model_version_id if definition else None, "prompt_set_id": definition.prompt_set_id if definition else None, "parameters": definition.parameters if definition else {}, "checkpoint_revision_id": (db.get(models.ModelVersion, definition.model_version_id).checkpoint_revision_id if definition and definition.model_version_id and db.get(models.ModelVersion, definition.model_version_id) else None), "cells": cell_rows, "jobs": [dump(job) for job in jobs]}


@router.post("/grid-definitions/{grid_id}/runs", status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
def enqueue_grid(grid_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    raise HTTPException(status_code=409, detail={"code": "FAL_ADMISSION_REQUIRED", "message": "Create and admit a FAL admission before provider work."})
    grid = get_or_404(db, models.GridDefinition, grid_id)
    actor = active_profile(db, x_profile_id)
    job = models.Job(workspace_id=current_workspace(db).id, kind="fal.grid_batch", profile_id=actor.id, payload={"grid_definition_id": grid.id})
    db.add(job)
    db.flush()
    record_activity(db, action="grid.queued", subject_type="grid_definition", subject_id=grid.id, profile_id=actor.id, project_id=grid.project_id, details={"job_id": job.id})
    db.commit()
    return {"grid_definition_id": grid.id, "job_id": job.id, "state": job.state.value}
