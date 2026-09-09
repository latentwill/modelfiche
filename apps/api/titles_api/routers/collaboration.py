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
    return {column.name: getattr(row, row.__mapper__.get_property_by_column(column).key) for column in row.__table__.columns}

def prompt_projection(prompt: models.Prompt) -> dict[str, Any]:
    """Serialize the mapped metadata_ attribute instead of SQLAlchemy's class metadata."""
    return {
        "id": prompt.id,
        "prompt_set_id": prompt.prompt_set_id,
        "text": prompt.text,
        "position": prompt.position,
        "metadata": dict(prompt.metadata_ or {}),
        "created_at": prompt.created_at,
        "updated_at": prompt.updated_at,
    }


def attributed(
    db: Session,
    row,
    profiles: dict[str, models.UserProfile] | None = None,
) -> dict[str, Any]:
    profile = (
        profiles.get(row.profile_id)
        if profiles is not None and row.profile_id
        else (db.get(models.UserProfile, row.profile_id) if row.profile_id else None)
    )
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


def _entity_reference(db: Session, kind: str, identifier: str) -> dict[str, Any]:
    if kind == "training_run":
        row = db.get(models.TrainingRun, identifier)
        return {"label": row.name if row else identifier, "href": f"#/run/{identifier}" if row else None}
    if kind == "checkpoint":
        row = db.get(models.Checkpoint, identifier)
        return {"label": f"Checkpoint step {row.step}" if row else identifier, "href": f"#/checkpoint/{identifier}" if row else None}
    if kind == "model":
        row = db.get(models.Model, identifier)
        return {"label": row.name if row else identifier, "href": f"#/model/{identifier}" if row else None}
    if kind == "model_version":
        row = db.get(models.ModelVersion, identifier)
        return {"label": row.name if row else identifier, "href": f"#/model-version/{identifier}" if row else None}
    if kind == "dataset_version":
        row = db.get(models.DatasetVersion, identifier)
        dataset = db.get(models.Dataset, row.dataset_id) if row else None
        return {
            "label": f"{dataset.name} · {row.name}" if row and dataset else row.name if row else identifier,
            "href": f"#/dataset/{dataset.id}" if dataset else None,
        }
    if kind == "asset":
        row = db.get(models.Asset, identifier)
        return {"label": row.name if row else identifier, "href": f"#/gallery?asset={identifier}" if row else None}
    return {"label": identifier, "href": None}


def _derived_edge(
    db: Session,
    *,
    source_type: str,
    source_id: str,
    target_type: str,
    target_id: str,
    relationship: str,
    metadata: dict[str, Any] | None = None,
    created_at: Any = None,
) -> dict[str, Any]:
    source = _entity_reference(db, source_type, source_id)
    target = _entity_reference(db, target_type, target_id)
    return {
        "id": f"derived:{source_type}:{source_id}:{relationship}:{target_type}:{target_id}",
        "source_type": source_type,
        "source_id": source_id,
        "source_label": source["label"],
        "source_href": source["href"],
        "target_type": target_type,
        "target_id": target_id,
        "target_label": target["label"],
        "target_href": target["href"],
        "relationship": relationship,
        "metadata": metadata or {},
        "created_at": created_at,
        "derived": True,
    }


def _checkpoint_chain(db: Session, checkpoint: models.Checkpoint) -> list[dict[str, Any]]:
    run = db.get(models.TrainingRun, checkpoint.run_id)
    sample_count = int(
        db.scalar(
            select(func.count())
            .select_from(models.Sample)
            .where(models.Sample.checkpoint_id == checkpoint.id)
        )
        or 0
    )
    result = [
        _derived_edge(
            db,
            source_type="training_run",
            source_id=checkpoint.run_id,
            target_type="checkpoint",
            target_id=checkpoint.id,
            relationship="produced",
            metadata={"step": checkpoint.step, "sample_count": sample_count},
            created_at=checkpoint.created_at,
        )
    ]
    version = db.scalar(select(models.ModelVersion).where(models.ModelVersion.checkpoint_id == checkpoint.id))
    if version:
        result.append(
            _derived_edge(
                db,
                source_type="checkpoint",
                source_id=checkpoint.id,
                target_type="model_version",
                target_id=version.id,
                relationship="registered_as",
                metadata={"lifecycle_state": version.lifecycle_state, "model_id": version.model_id},
                created_at=version.created_at,
            )
        )
    if run and run.dataset_version_id:
        result.append(
            _derived_edge(
                db,
                source_type="dataset_version",
                source_id=run.dataset_version_id,
                target_type="training_run",
                target_id=run.id,
                relationship="trained_with",
                created_at=run.created_at,
            )
        )
    return result


def _merge_component_edges(db: Session, run: models.TrainingRun) -> list[dict[str, Any]]:
    manifest = run.raw_manifest if isinstance(run.raw_manifest, dict) else {}
    components = manifest.get("components")
    if manifest.get("operation") != "checkpoint_merge" or not isinstance(components, dict):
        return []
    training_config = (run.normalized_config or {}).get("training_config", {})
    transformer_weights = training_config.get("transformer_weights", {}) if isinstance(training_config, dict) else {}
    text_fusion_weights = training_config.get("text_fusion_weights", {}) if isinstance(training_config, dict) else {}
    result = []
    for component, raw_step in components.items():
        try:
            step = int(raw_step)
        except (TypeError, ValueError):
            continue
        candidates = db.execute(
            select(models.Checkpoint, models.TrainingRun, models.Asset)
            .join(models.TrainingRun, models.TrainingRun.id == models.Checkpoint.run_id)
            .join(models.Asset, models.Asset.id == models.Checkpoint.asset_id)
            .where(
                models.TrainingRun.project_id == run.project_id,
                models.TrainingRun.id != run.id,
                models.Checkpoint.step == step,
            )
        ).all()
        token = str(component).lower()
        ranked = sorted(
            candidates,
            key=lambda item: (
                token in str(item[2].name or "").lower(),
                token in str(item[1].name or "").lower(),
                item[0].created_at,
            ),
            reverse=True,
        )
        checkpoint = ranked[0][0] if ranked else None
        metadata = {
            "component": component,
            "step": step,
            "transformer_weight": transformer_weights.get(component),
            "text_fusion_weight": text_fusion_weights.get(component),
            "resolved": checkpoint is not None,
        }
        if checkpoint:
            result.append(
                _derived_edge(
                    db,
                    source_type="checkpoint",
                    source_id=checkpoint.id,
                    target_type="training_run",
                    target_id=run.id,
                    relationship="merged_into",
                    metadata=metadata,
                    created_at=run.created_at,
                )
            )
        else:
            result.append({
                "id": f"derived:checkpoint_reference:{component}:{step}:merged_into:training_run:{run.id}",
                "source_type": "checkpoint_reference",
                "source_id": f"{component}:{step}",
                "source_label": f"{component} checkpoint · step {step}",
                "source_href": None,
                "target_type": "training_run",
                "target_id": run.id,
                "target_label": run.name,
                "target_href": f"#/run/{run.id}",
                "relationship": "merged_into",
                "metadata": metadata,
                "created_at": run.created_at,
                "derived": True,
            })
    return result


def _derived_lineage(db: Session, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
    if subject_type == "training_run":
        run = db.get(models.TrainingRun, subject_id)
        if not run:
            return []
        checkpoints = db.scalars(
            select(models.Checkpoint)
            .where(models.Checkpoint.run_id == run.id)
            .order_by(models.Checkpoint.step)
        ).all()
        edges = [edge for checkpoint in checkpoints for edge in _checkpoint_chain(db, checkpoint)]
        if run.dataset_version_id and not checkpoints:
            edges.append(
                _derived_edge(
                    db,
                    source_type="dataset_version",
                    source_id=run.dataset_version_id,
                    target_type="training_run",
                    target_id=run.id,
                    relationship="trained_with",
                    created_at=run.created_at,
                )
            )
        return [*edges, *_merge_component_edges(db, run)]
    if subject_type == "checkpoint":
        checkpoint = db.get(models.Checkpoint, subject_id)
        return _checkpoint_chain(db, checkpoint) if checkpoint else []
    if subject_type == "model_version":
        version = db.get(models.ModelVersion, subject_id)
        checkpoint = db.get(models.Checkpoint, version.checkpoint_id) if version else None
        return _checkpoint_chain(db, checkpoint) if checkpoint else []
    if subject_type == "model":
        versions = db.scalars(
            select(models.ModelVersion)
            .where(models.ModelVersion.model_id == subject_id)
            .order_by(models.ModelVersion.created_at.desc())
        ).all()
        return [
            _derived_edge(
                db,
                source_type="model",
                source_id=subject_id,
                target_type="model_version",
                target_id=version.id,
                relationship="has_version",
                metadata={"lifecycle_state": version.lifecycle_state},
                created_at=version.created_at,
            )
            for version in versions
        ]
    return []


def _enrich_lineage_edge(db: Session, edge: models.LineageEdge) -> dict[str, Any]:
    source = _entity_reference(db, edge.source_type, edge.source_id)
    target = _entity_reference(db, edge.target_type, edge.target_id)
    return {
        **dump(edge),
        "source_label": source["label"],
        "source_href": source["href"],
        "target_label": target["label"],
        "target_href": target["href"],
        "preview_asset_id": _lineage_preview(db, edge),
        "derived": False,
    }


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
def reviews(
    db: DB,
    subject_type: str | None = None,
    subject_id: str | None = None,
    subject: str | None = None,
    project_id: str | None = None,
    decision: str | None = None,
    profile_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    query = select(models.Review).where(
        models.Review.workspace_id == current_workspace(db).id
    ).order_by(models.Review.created_at.desc())
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
    if project_id:
        subject_ids = select(models.ActivityEvent.subject_id).where(models.ActivityEvent.project_id == project_id)
        query = query.where(models.Review.subject_id.in_(subject_ids))
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    profiles = {
        profile.id: profile
        for profile in db.scalars(
            select(models.UserProfile).where(
                models.UserProfile.id.in_({row.profile_id for row in rows if row.profile_id})
            )
        ).all()
    }
    return [attributed(db, row, profiles) for row in rows]

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
def comments(
    db: DB,
    subject_type: str | None = None,
    subject_id: str | None = None,
    subject: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    if subject and ":" in subject:
        subject_type, subject_id = subject.split(":", 1)
    if not subject_type or not subject_id:
        raise HTTPException(status_code=422, detail="subject_type and subject_id, or subject, are required")
    query = select(models.Comment).where(
        models.Comment.workspace_id == current_workspace(db).id,
        models.Comment.subject_type == subject_type,
        models.Comment.subject_id == subject_id,
    ).order_by(models.Comment.created_at)
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    profiles = {
        profile.id: profile
        for profile in db.scalars(
            select(models.UserProfile).where(
                models.UserProfile.id.in_({row.profile_id for row in rows if row.profile_id})
            )
        ).all()
    }
    return [attributed(db, row, profiles) for row in rows]


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
def lineage(db: DB, subject_type: str, subject_id: str, depth: int = Query(3, ge=1, le=5)):
    del depth  # The current typed projection is deliberately bounded to the persisted run/model chain.
    query = select(models.LineageEdge).where(or_(
        (models.LineageEdge.source_type == subject_type) & (models.LineageEdge.source_id == subject_id),
        (models.LineageEdge.target_type == subject_type) & (models.LineageEdge.target_id == subject_id),
    )).order_by(models.LineageEdge.created_at)
    persisted = [_enrich_lineage_edge(db, row) for row in db.scalars(query).all()]
    derived = _derived_lineage(db, subject_type, subject_id)
    seen = {
        (edge["source_type"], edge["source_id"], edge["relationship"], edge["target_type"], edge["target_id"])
        for edge in persisted
    }
    return [
        *persisted,
        *[
            edge
            for edge in derived
            if (edge["source_type"], edge["source_id"], edge["relationship"], edge["target_type"], edge["target_id"]) not in seen
        ],
    ]


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


_GENERATION_FILENAME_FIELDS = frozenset({"summary", "filename", "file_name"})


def _generation_eval_id(row: models.ActivityEvent) -> str | None:
    if row.action != "asset.generated":
        return None
    details = row.details if isinstance(row.details, dict) else {}
    value = details.get("eval_run_id")
    return str(value) if value else None


def _generation_details(
    generated: models.ActivityEvent,
    admitted: models.ActivityEvent | None,
) -> dict[str, Any]:
    admitted_details = admitted.details if admitted and isinstance(admitted.details, dict) else {}
    generated_details = generated.details if isinstance(generated.details, dict) else {}
    details = {
        key: value
        for key, value in generated_details.items()
        if key not in _GENERATION_FILENAME_FIELDS
    }
    details.update({
        key: value
        for key, value in admitted_details.items()
        if key not in _GENERATION_FILENAME_FIELDS
    })
    return details


def _looks_like_image_filename(value: Any) -> bool:
    return isinstance(value, str) and value.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")
    )


@router.get("/activity")
def activity(db: DB, project_id: str | None = None, subject_type: str | None = None, subject_id: str | None = None, profile_id: str | None = None, since: str | None = None, limit: int = Query(100, le=500)):
    query = select(models.ActivityEvent).where(
        models.ActivityEvent.workspace_id == current_workspace(db).id,
    ).order_by(models.ActivityEvent.created_at.desc())
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
    rows = db.scalars(query.limit(min(max(limit, 1) * 2, 500))).all()
    generated_eval_ids = {
        eval_id
        for row in rows
        if (eval_id := _generation_eval_id(row)) is not None
    }
    admitted_rows = [
        row
        for row in rows
        if row.action == "fal.admission_admitted"
        and isinstance(row.details, dict)
        and row.details.get("eval_run_id") in generated_eval_ids
    ]
    admitted_eval_ids = {
        str(row.details["eval_run_id"])
        for row in admitted_rows
        if isinstance(row.details, dict) and row.details.get("eval_run_id")
    }
    missing_admitted_eval_ids = generated_eval_ids - admitted_eval_ids
    if missing_admitted_eval_ids:
        admitted_query = select(models.ActivityEvent).where(
            models.ActivityEvent.workspace_id == current_workspace(db).id,
            models.ActivityEvent.action == "fal.admission_admitted",
            models.ActivityEvent.details["eval_run_id"].as_string().in_(sorted(missing_admitted_eval_ids)),
        ).order_by(models.ActivityEvent.created_at.desc())
        if project_id:
            admitted_query = admitted_query.where(models.ActivityEvent.project_id == project_id)
        admitted_rows.extend(db.scalars(admitted_query).all())
    admitted_by_eval_id: dict[str, models.ActivityEvent] = {}
    for row in admitted_rows:
        details = row.details if isinstance(row.details, dict) else {}
        eval_id = details.get("eval_run_id")
        if eval_id and str(eval_id) not in admitted_by_eval_id:
            admitted_by_eval_id[str(eval_id)] = row

    enrichment_rows = [*rows, *admitted_rows]
    profile_ids = {row.profile_id for row in enrichment_rows if row.profile_id}
    names = {
        profile.id: profile.display_name
        for profile in db.scalars(select(models.UserProfile).where(models.UserProfile.id.in_(profile_ids))).all()
    } if profile_ids else {}
    enrichment = _activity_enrichment(db, enrichment_rows)
    generated_rows = [row for row in rows if _generation_eval_id(row)]
    hidden_admission_ids = {
        admitted_by_eval_id[eval_id].id
        for row in generated_rows
        if (eval_id := _generation_eval_id(row)) in admitted_by_eval_id
    }
    result = []
    for row in rows:
        if row.id in hidden_admission_ids:
            continue
        details = row.details if isinstance(row.details, dict) else {}
        eval_id = _generation_eval_id(row)
        admitted = admitted_by_eval_id.get(eval_id) if eval_id else None
        payload = {
            **dump(row),
            "event_type": row.action,
            "summary": details.get("summary") or (
                "Generation admitted"
                if row.action == "fal.admission_admitted"
                else row.action.replace(".", " ").replace("_", " ").title()
            ),
            "profile_name": names.get(row.profile_id),
            **enrichment.get(({"training_run": "run", "import_job": "import_job"}.get(row.subject_type, row.subject_type), row.subject_id), {
                "object_href": None,
                "object_label": None,
                "preview_asset_id": None,
            }),
        }
        if row.action == "asset.generated":
            generated_enrichment = enrichment.get(("asset", row.subject_id), {})
            admitted_enrichment = enrichment.get(("fal_admission", admitted.subject_id), {}) if admitted else {}
            generated_preview_id = generated_enrichment.get("preview_asset_id")
            object_label = admitted_enrichment.get("object_label") or generated_enrichment.get("object_label")
            if _looks_like_image_filename(object_label):
                object_label = "Generated image" if generated_preview_id else None
            payload.update({
                "action": "image.generated",
                "event_type": "image.generated",
                "summary": "Image generated",
                "details": _generation_details(row, admitted),
                "profile_id": admitted.profile_id if admitted else row.profile_id,
                "profile_name": names.get(admitted.profile_id if admitted else row.profile_id),
                "object_href": admitted_enrichment.get("object_href") or generated_enrichment.get("object_href"),
                "object_label": object_label,
                "preview_asset_id": generated_preview_id or admitted_enrichment.get("preview_asset_id"),
            })
        result.append(payload)
    return result[:max(limit, 1)]




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




@router.get("/prompt-sets")
def prompt_sets(db: DB, project_id: str | None = None):
    query = select(models.PromptSet).join(models.Project, models.Project.id == models.PromptSet.project_id).where(
        models.Project.workspace_id == current_workspace(db).id,
    ).order_by(models.PromptSet.updated_at.desc())
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
    return {**dump(prompt_set), "prompts": [prompt_projection(prompt) for prompt in prompts]}








@router.get("/eval-definitions")
def eval_definitions(db: DB, project_id: str | None = None):
    query = select(models.EvalDefinition).join(models.Project, models.Project.id == models.EvalDefinition.project_id).where(
        models.Project.workspace_id == current_workspace(db).id,
    ).order_by(models.EvalDefinition.updated_at.desc())
    if project_id:
        query = query.where(models.EvalDefinition.project_id == project_id)
    return [dump(row) for row in db.scalars(query).all()]


@router.get("/eval-definitions/{definition_id}")
def eval_definition(definition_id: str, db: DB):
    return dump(get_or_404(db, models.EvalDefinition, definition_id))




def _eval_output_provenance(db: Session, output: models.EvalOutput, cache: dict[str, Any]) -> dict[str, Any]:
    provider_metadata = dict(output.provider_metadata or {})
    grid_metadata = dict(provider_metadata.get("grid_metadata") or {})
    asset = db.get(models.Asset, output.asset_id)
    asset_metadata = dict(asset.metadata_ or {}) if asset else {}
    version_id = str(grid_metadata.get("model_version_id") or asset_metadata.get("model_version_id") or "")

    resolved = cache.get(f"version:{version_id}") if version_id else None
    if resolved is None:
        version = db.get(models.ModelVersion, version_id) if version_id else None
        model = db.get(models.Model, version.model_id) if version else None
        checkpoint = db.get(models.Checkpoint, version.checkpoint_id) if version else None
        training_run = db.get(models.TrainingRun, checkpoint.run_id) if checkpoint else None
        merge = db.scalar(
            select(models.MergeOperation).where(
                models.MergeOperation.output_checkpoint_revision_id == version.checkpoint_revision_id
            )
        ) if version and version.checkpoint_revision_id else None
        resolved = (version, model, checkpoint, training_run, merge)
        if version_id:
            cache[f"version:{version_id}"] = resolved
    version, model, checkpoint, training_run, merge = resolved

    grid_id = str(grid_metadata.get("grid_definition_id") or "")
    grid = cache.get(f"grid:{grid_id}") if grid_id else None
    if grid_id and grid is None:
        grid = db.get(models.GridDefinition, grid_id)
        cache[f"grid:{grid_id}"] = grid
    axis_values: dict[str, Any] = {}
    if grid:
        snapshot = dict(grid.plan_snapshot or {})
        axes = dict(snapshot.get("axes") or {})
        coordinates = dict(grid_metadata.get("coordinates") or {})
        for coordinate in ("x", "y", "z"):
            axis = axes.get(coordinate)
            index = coordinates.get(coordinate)
            if not isinstance(axis, dict) or index is None:
                continue
            values = list(axis.get("values") or [])
            if isinstance(index, int) and 0 <= index < len(values):
                axis_values[str(axis.get("name") or coordinate)] = values[index]

    model_name = model.name if model else grid_metadata.get("model_name")
    version_name = version.name if version else grid_metadata.get("model_version_name")
    checkpoint_step = checkpoint.step if checkpoint else grid_metadata.get("checkpoint_step")
    result = {
        "kind": "merge" if merge else "checkpoint",
        "axis_values": axis_values,
        "model_id": model.id if model else grid_metadata.get("model_id"),
        "model_name": model_name,
        "model_version_id": version.id if version else version_id or None,
        "model_version_name": version_name,
        "checkpoint_id": checkpoint.id if checkpoint else grid_metadata.get("checkpoint_id"),
        "checkpoint_revision_id": version.checkpoint_revision_id if version else grid_metadata.get("checkpoint_revision_id"),
        "checkpoint_step": checkpoint_step,
        "base_model": (version.base_model if version else None) or grid_metadata.get("base_model"),
    }
    if merge:
        result.update({
            "merge_operation_id": merge.id,
            "merge_operator": merge.operator,
            "merge_notation": merge.compact_notation,
            "merge_recipe": dict(merge.recipe or {}),
            "merge_schema_version": merge.schema_version,
        })
    else:
        result.update({
            "training_run_id": training_run.id if training_run else grid_metadata.get("run_id"),
            "training_run_name": training_run.name if training_run else grid_metadata.get("run_name"),
        })
    return result


@router.get("/eval-runs/{run_id}")
def eval_run(run_id: str, db: DB):
    run = get_or_404(db, models.EvalRun, run_id)
    definition = get_or_404(db, models.EvalDefinition, run.definition_id)
    outputs = db.scalars(select(models.EvalOutput).where(models.EvalOutput.eval_run_id == run.id).order_by(models.EvalOutput.created_at)).all()
    jobs = db.scalars(select(models.Job).where(models.Job.payload["eval_run_id"].as_string() == run.id).order_by(models.Job.created_at.desc())).all()
    latest_job = jobs[0] if jobs else None
    output_rows = []
    provenance_cache: dict[str, Any] = {}
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
                "provenance": _eval_output_provenance(db, output, provenance_cache),
            }
        )
    version = db.get(models.ModelVersion, definition.model_version_id) if definition.model_version_id else None
    model = db.get(models.Model, version.model_id) if version else None
    prompt_set = db.get(models.PromptSet, definition.prompt_set_id) if definition.prompt_set_id else None
    return {**dump(run), "name": definition.name, "project_id": definition.project_id, "endpoint_adapter": definition.endpoint,
        "model_version_id": definition.model_version_id, "model_version_name": version.name if version else None,
        "model_name": model.name if model else None, "prompt_set_id": definition.prompt_set_id,
        "prompt_set_name": prompt_set.name if prompt_set else None, "prompt_set_version": prompt_set.version if prompt_set else None,
        "inline_prompts": definition.inline_prompts, "prompt_count": len(definition.inline_prompts or []),
        "parameters": run.parameters_snapshot, "progress": latest_job.progress if latest_job else (1 if run.status == "succeeded" else 0), "job_id": latest_job.id if latest_job else None, "outputs": output_rows}


@router.get("/eval-runs")
def eval_runs(db: DB, definition_id: str | None = None, project_id: str | None = None, run_status: str | None = Query(None, alias="status")):
    query = select(models.EvalRun).join(models.EvalDefinition, models.EvalDefinition.id == models.EvalRun.definition_id).join(
        models.Project, models.Project.id == models.EvalDefinition.project_id,
    ).where(models.Project.workspace_id == current_workspace(db).id).order_by(models.EvalRun.created_at.desc())
    if definition_id:
        query = query.where(models.EvalRun.definition_id == definition_id)
    if run_status:
        query = query.where(models.EvalRun.status == run_status)
    if project_id:
        query = query.where(models.EvalDefinition.project_id == project_id)
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
    provenance_cache: dict[str, Any] = {}
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
                "provenance": _eval_output_provenance(db, output, provenance_cache),
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




@router.get("/grid-definitions")
def grid_definitions(db: DB, project_id: str | None = None):
    query = select(models.GridDefinition).join(models.Project, models.Project.id == models.GridDefinition.project_id).where(
        models.Project.workspace_id == current_workspace(db).id,
    ).order_by(models.GridDefinition.created_at.desc())
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
