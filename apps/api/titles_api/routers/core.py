from typing import Annotated, Any

from pathlib import Path
import re
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import models, schemas
from ..database import get_db
from ..services import active_profile, current_workspace, get_or_404, record_activity, resolve_entity_workspace, workspace_object
from ..image_provenance import normalize_fal_image_metadata
from ..asset_cache import AssetCache
from ..integrations.config import CacheSettings
from ..settings import get_settings
from ..integrations.s3.browser import PrefixAccessError, normalize_prefix
from ..storage.repository import StorageRepository

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def profile_for(db: Session, profile_id: str | None) -> models.UserProfile:
    return active_profile(db, profile_id)

def _s3_object_key(location: schemas.AssetLocationCreate) -> str | None:
    if location.object_key:
        return normalize_prefix(location.object_key).rstrip("/")
    parsed = urlparse(location.uri)
    if parsed.scheme == "s3":
        return normalize_prefix(unquote(parsed.path.lstrip("/"))).rstrip("/")
    return None


def _source_allows_key(source: models.ImportSource, key: str) -> bool:
    roots = tuple(normalize_prefix(value).rstrip("/") for value in (source.allowed_prefixes or ()))
    return not roots or any(key == root or key.startswith(f"{root}/") for root in roots)


def _resolve_s3_source(
    db: Session,
    workspace_id: str,
    location: schemas.AssetLocationCreate,
) -> tuple[models.ImportSource | None, str | None]:
    if location.provider != "s3":
        return None, None
    try:
        key = _s3_object_key(location)
    except PrefixAccessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    candidates = list(
        db.scalars(
            select(models.ImportSource).where(
                models.ImportSource.workspace_id == workspace_id,
                models.ImportSource.provider == "s3",
                models.ImportSource.is_active.is_(True),
                models.ImportSource.bucket == location.bucket,
            )
        )
    ) if location.bucket else []
    if location.source_id:
        source = db.scalar(
            select(models.ImportSource).where(
                models.ImportSource.id == location.source_id,
                models.ImportSource.workspace_id == workspace_id,
                models.ImportSource.provider == "s3",
                models.ImportSource.is_active.is_(True),
            )
        )
        if source is None:
            raise HTTPException(status_code=422, detail="active S3 source is unavailable")
        if location.bucket and location.bucket != source.bucket:
            raise HTTPException(status_code=422, detail="S3 location bucket does not match its source")
        if key is None or not _source_allows_key(source, key):
            raise HTTPException(status_code=422, detail="S3 object is outside the source's allowed prefixes")
        return source, key
    if key is None:
        return None, None
    allowed = [source for source in candidates if _source_allows_key(source, key)]
    if len(allowed) == 1:
        return allowed[0], key
    if len(candidates) == 1:
        raise HTTPException(status_code=422, detail="S3 object is outside the source's allowed prefixes")
    return None, key


def available_workspace_slug(db: Session, name: str) -> str:
    base = models.workspace_slug(name)
    used = set(
        db.scalars(
            select(models.Workspace.slug).where(models.Workspace.slug.like(f"{base}%"))
        )
    )
    if base not in used:
        return base
    suffix = 2
    while True:
        ending = f"-{suffix}"
        candidate = f"{base[: 80 - len(ending)]}{ending}"
        if candidate not in used:
            return candidate
        suffix += 1


@router.post("/workspaces", response_model=schemas.WorkspaceOut, status_code=status.HTTP_201_CREATED)
def create_workspace(body: schemas.WorkspaceCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    actor = active_profile(db, x_profile_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="workspace name cannot be blank")
    workspace = models.Workspace(name=name, slug=available_workspace_slug(db, name))
    db.add(workspace)
    db.flush()
    profile = models.UserProfile(
        workspace_id=workspace.id,
        display_name=actor.display_name,
        email=actor.email,
        initials=actor.initials,
        avatar_color=actor.avatar_color,
        is_active=True,
    )
    db.add(profile)
    db.flush()
    record_activity(
        db,
        action="workspace.created",
        subject_type="workspace",
        subject_id=workspace.id,
        profile_id=profile.id,
        workspace_id=workspace.id,
    )
    db.commit()
    return workspace


@router.get("/workspaces", response_model=list[schemas.WorkspaceOut])
def workspaces(db: DB):
    return db.scalars(select(models.Workspace).order_by(models.Workspace.created_at, models.Workspace.id)).all()

@router.get("/workspace-resolutions/{entity_type}/{entity_id}")
def workspace_resolution(entity_type: str, entity_id: str, db: DB):
    """Resolve an unscoped legacy entity link to its canonical workspace."""
    workspace = resolve_entity_workspace(db, entity_type, entity_id)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "workspace_slug": workspace.slug,
    }


@router.patch("/workspaces/{workspace_id}", response_model=schemas.WorkspaceOut)
def update_workspace(workspace_id: str, body: schemas.WorkspaceUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    workspace = get_or_404(db, models.Workspace, workspace_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="workspace name cannot be blank")
    workspace.name = name
    record_activity(db, action="workspace.updated", subject_type="workspace", subject_id=workspace.id, profile_id=profile_for(db, x_profile_id).id)
    db.commit()
    return workspace


@router.get("/profiles", response_model=list[schemas.ProfileOut])
def profiles(db: DB, include_archived: bool = False):
    query = select(models.UserProfile).where(models.UserProfile.workspace_id == current_workspace(db).id).order_by(models.UserProfile.display_name)
    if not include_archived:
        query = query.where(models.UserProfile.is_active.is_(True))
    return db.scalars(query).all()


@router.post("/profiles", response_model=schemas.ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(body: schemas.ProfileCreate, db: DB):
    profile = models.UserProfile(workspace_id=current_workspace(db).id, **body.model_dump())
    db.add(profile)
    db.flush()
    record_activity(db, action="profile.created", subject_type="profile", subject_id=profile.id, profile_id=profile.id)
    db.commit()
    return profile


@router.get("/profiles/active", response_model=schemas.ProfileOut)
def selected_profile(db: DB):
    return active_profile(db, None)


@router.get("/profiles/{profile_id}", response_model=schemas.ProfileOut)
def profile(profile_id: str, db: DB):
    return workspace_object(db, models.UserProfile, profile_id)


@router.patch("/profiles/{profile_id}", response_model=schemas.ProfileOut)
def update_profile(profile_id: str, body: schemas.ProfileUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    profile = workspace_object(db, models.UserProfile, profile_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(profile, key, value)
    record_activity(db, action="profile.updated", subject_type="profile", subject_id=profile.id, profile_id=profile_for(db, x_profile_id).id)
    db.commit()
    return profile


@router.put("/profiles/{profile_id}/active", response_model=schemas.ProfileOut)
def use_profile(profile_id: str, db: DB):
    profile = workspace_object(db, models.UserProfile, profile_id)
    if not profile.is_active:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="archived profile cannot be active")
    pref = db.scalar(select(models.LocalPreference).where(models.LocalPreference.key == "active_profile"))
    if pref is None:
        pref = models.LocalPreference(key="active_profile", value={"profile_id": profile.id})
        db.add(pref)
    else:
        pref.value = {"profile_id": profile.id}
    db.commit()
    return profile


@router.post("/profiles/{profile_id}/archive", response_model=schemas.ProfileOut)
def archive_profile(profile_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    profile = workspace_object(db, models.UserProfile, profile_id)
    actor = profile_for(db, x_profile_id)
    active_count = int(db.scalar(select(func.count()).select_from(models.UserProfile).where(
        models.UserProfile.workspace_id == current_workspace(db).id,
        models.UserProfile.is_active.is_(True),
    )) or 0)
    if profile.is_active and active_count <= 1:
        raise HTTPException(status_code=409, detail="at least one active profile is required")
    preference = db.scalar(select(models.LocalPreference).where(models.LocalPreference.key == "active_profile"))
    if preference and preference.value.get("profile_id") == profile.id:
        raise HTTPException(status_code=409, detail="select another active profile before archiving this one")
    profile.is_active = False
    record_activity(db, action="profile.archived", subject_type="profile", subject_id=profile.id, profile_id=actor.id)
    db.commit()
    return profile


@router.get("/projects", response_model=list[schemas.ProjectOut])
def projects(db: DB, state: str | None = None, include_archived: bool = False):
    query = select(models.Project).where(models.Project.workspace_id == current_workspace(db).id).order_by(models.Project.updated_at.desc())
    if state:
        query = query.where(models.Project.state == state)
    elif not include_archived:
        query = query.where(models.Project.state != "archived")
    return db.scalars(query).all()


@router.post("/projects", response_model=schemas.ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(body: schemas.ProjectCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    actor = profile_for(db, x_profile_id)
    project = models.Project(workspace_id=current_workspace(db).id, **body.model_dump())
    db.add(project)
    db.flush()
    record_activity(db, action="project.created", subject_type="project", subject_id=project.id, profile_id=actor.id, project_id=project.id)
    db.commit()
    return project


@router.get("/projects/{project_id}", response_model=schemas.ProjectOut)
def project(project_id: str, db: DB):
    return workspace_object(db, models.Project, project_id)


@router.patch("/projects/{project_id}", response_model=schemas.ProjectOut)
def update_project(project_id: str, body: schemas.ProjectUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    project = workspace_object(db, models.Project, project_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    record_activity(db, action="project.updated", subject_type="project", subject_id=project.id, profile_id=profile_for(db, x_profile_id).id, project_id=project.id)
    db.commit()
    return project


@router.get("/assets", response_model=list[schemas.AssetOut])
def assets(db: DB, project_id: str | None = None, kind: str | None = None, q: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    query = select(models.Asset).options(selectinload(models.Asset.locations)).where(models.Asset.workspace_id == current_workspace(db).id).order_by(models.Asset.created_at.desc())
    if kind:
        query = query.where(models.Asset.kind == kind)
    if project_id:
        query = query.where(models.Asset.project_id == project_id)
        if kind == "image":
            query = query.where(or_(
                models.Asset.mime_type.startswith("image/"),
                models.Asset.metadata_["category"].as_string().in_(["sample", "eval_output", "dataset_image"]),
            ))
    if q:
        query = query.where(models.Asset.name.contains(q))
    return db.scalars(query.offset(offset).limit(limit)).all()


def _gallery_query(
    db: Session,
    project_id: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    decision: str | None = None,
    rating: int | None = None,
    model_id: str | None = None,
    dataset_id: str | None = None,
    eval_run_id: str | None = None,
    category: str | None = None,
    provider: str | None = None,
    include_dataset_assets: bool = False,
    sort: str = "origin_time_desc",
):
    workspace_id = current_workspace(db).id
    eval_time = select(models.EvalOutput.generated_at).where(models.EvalOutput.asset_id == models.Asset.id).order_by(models.EvalOutput.generated_at.desc()).limit(1).scalar_subquery()
    sample_time = select(models.Sample.modified_at).where(models.Sample.asset_id == models.Asset.id).order_by(models.Sample.modified_at.desc()).limit(1).scalar_subquery()
    is_eval = or_(select(models.EvalOutput.id).where(models.EvalOutput.asset_id == models.Asset.id).exists(), models.Asset.metadata_["category"].as_string() == "eval_output")
    is_sample = or_(select(models.Sample.id).where(models.Sample.asset_id == models.Asset.id).exists(), models.Asset.metadata_["category"].as_string() == "sample")
    # Canonical origin timestamps are intentionally not substituted: imported
    # EVAL/SAMPLE rows without their origin time sort last with an ID tie-break.
    canonical_time = case((is_eval, eval_time), (is_sample, sample_time), else_=models.Asset.created_at)
    order = canonical_time.asc().nulls_last() if sort == "origin_time_asc" else canonical_time.desc().nulls_last()
    # created_at is only a deterministic tie-break inside the missing-time group;
    # it is never exposed or labelled as generated_at/modified_at.
    query = select(models.Asset).where(models.Asset.workspace_id == workspace_id).order_by(order, models.Asset.created_at.desc(), models.Asset.id)
    if not include_dataset_assets and not dataset_id:
        dataset_asset_ids = select(models.DatasetItem.asset_id)
        query = query.where(
            models.Asset.id.not_in(dataset_asset_ids),
            func.coalesce(models.Asset.metadata_["category"].as_string(), "") != "dataset_image",
        )
    if project_id and category != "sample":
        query = query.where(models.Asset.project_id == project_id)
    if category == "sample":
        sample_asset_ids = select(models.Sample.asset_id)
        if project_id:
            sample_asset_ids = sample_asset_ids.join(
                models.TrainingRun, models.TrainingRun.id == models.Sample.run_id
            ).where(models.TrainingRun.project_id == project_id)
        query = query.where(models.Asset.id.in_(sample_asset_ids))
    elif category == "eval_output":
        # Historical imports predate relational EvalOutput rows. Gallery keeps them
        # navigable by their canonical origin tag; Eval pages remain run-owned.
        query = query.where(or_(models.Asset.id.in_(select(models.EvalOutput.asset_id)), models.Asset.metadata_["category"].as_string() == "eval_output"))
    elif category == "dataset_image":
        query = query.where(models.Asset.id.in_(select(models.DatasetItem.asset_id)))
    elif category:
        query = query.where(models.Asset.metadata_["category"].as_string() == category)
    if provider:
        query = query.where(models.Asset.metadata_["provider"].as_string() == provider)
    if model_id:
        eval_asset_ids = (
            select(models.EvalOutput.asset_id)
            .join(models.EvalRun, models.EvalRun.id == models.EvalOutput.eval_run_id)
            .join(models.EvalDefinition, models.EvalDefinition.id == models.EvalRun.definition_id)
            .join(models.ModelVersion, models.ModelVersion.id == models.EvalDefinition.model_version_id)
            .where(models.ModelVersion.model_id == model_id)
        )
        sample_asset_ids = (
            select(models.Sample.asset_id)
            .join(models.Checkpoint, models.Checkpoint.id == models.Sample.checkpoint_id)
            .join(models.ModelVersion, models.ModelVersion.checkpoint_id == models.Checkpoint.id)
            .where(models.ModelVersion.model_id == model_id)
        )
        linked_ids = eval_asset_ids.union(sample_asset_ids)
        query = query.where(models.Asset.id.in_(linked_ids))
    if dataset_id:
        dataset_asset_ids = (
            select(models.DatasetItem.asset_id)
            .join(models.DatasetVersion, models.DatasetVersion.id == models.DatasetItem.dataset_version_id)
            .where(models.DatasetVersion.dataset_id == dataset_id)
        )
        sample_asset_ids = (
            select(models.Sample.asset_id)
            .join(models.TrainingRun, models.TrainingRun.id == models.Sample.run_id)
            .join(models.DatasetVersion, models.DatasetVersion.id == models.TrainingRun.dataset_version_id)
            .where(models.DatasetVersion.dataset_id == dataset_id)
        )
        query = query.where(models.Asset.id.in_(dataset_asset_ids.union(sample_asset_ids)))
    if eval_run_id:
        query = query.where(models.Asset.id.in_(select(models.EvalOutput.asset_id).where(models.EvalOutput.eval_run_id == eval_run_id)))
    if kind:
        query = query.where(models.Asset.kind == kind)
        if kind == "image":
            query = query.where(or_(
                models.Asset.mime_type.startswith("image/"),
                models.Asset.metadata_["category"].as_string().in_(["sample", "eval_output", "dataset_image"]),
            ))
    if q:
        query = query.where(models.Asset.name.contains(q))
    if decision or rating is not None:
        reviewed_assets = select(models.Review.subject_id).where(models.Review.subject_type == "asset")
        if decision:
            reviewed_assets = reviewed_assets.where(models.Review.decision == decision)
        if rating is not None:
            reviewed_assets = reviewed_assets.where(models.Review.rating == rating)
        query = query.where(models.Asset.id.in_(reviewed_assets))
    return query


def _gallery_rows(db: Session, rows: list[models.Asset]) -> list[dict]:
    asset_ids = [row.id for row in rows]
    if not asset_ids:
        return []
    ranked_reviews = select(
        models.Review.subject_id,
        models.Review.rating,
        models.Review.decision,
        func.row_number().over(
            partition_by=models.Review.subject_id,
            order_by=(models.Review.created_at.desc(), models.Review.id.desc()),
        ).label("review_rank"),
    ).where(
        models.Review.subject_type == "asset",
        models.Review.subject_id.in_(asset_ids),
    ).subquery()
    reviews = {
        row["subject_id"]: row
        for row in db.execute(
            select(ranked_reviews).where(ranked_reviews.c.review_rank == 1)
        ).mappings()
    }
    review_counts = dict(db.execute(
        select(models.Review.subject_id, func.count())
        .where(models.Review.subject_type == "asset", models.Review.subject_id.in_(asset_ids))
        .group_by(models.Review.subject_id)
    ).all())
    comment_counts = dict(db.execute(
        select(models.Comment.subject_id, func.count())
        .where(models.Comment.subject_type == "asset", models.Comment.subject_id.in_(asset_ids))
        .group_by(models.Comment.subject_id)
    ).all())
    result = []
    for row in rows:
        review = reviews.get(row.id)
        rich_metadata = _gallery_asset_metadata(db, row)
        contract_metadata = schemas.GalleryMetadataOut.model_validate(rich_metadata).model_dump(mode="json", exclude_none=False)
        origin_type = contract_metadata.get("origin_type", "ASSET")
        result.append({
            "id": row.id,
            "asset_revision_id": row.id,
            "project_id": row.project_id,
            "kind": row.kind.value if hasattr(row.kind, "value") else row.kind,
            "name": row.name,
            "mime_type": row.mime_type,
            "sha256": row.sha256,
            "metadata": contract_metadata,
            "model_id": contract_metadata.get("model_id"),
            "model_name": contract_metadata.get("model_name"),
            "base_model": contract_metadata.get("base_model"),
            "dataset_id": contract_metadata.get("dataset_id"),
            "dataset_name": contract_metadata.get("dataset_name"),
            "run_id": contract_metadata.get("run_id"),
            "run_name": contract_metadata.get("run_name"),
            "step": contract_metadata.get("step"),
            "seed": contract_metadata.get("seed"),
            "locations": [schemas.AssetLocationOut.model_validate(location).model_dump(mode="json") for location in row.locations],
            "rating": review.rating if review else None,
            "decision": review.decision if review else None,
            "review_count": int(review_counts.get(row.id, 0)),
            "comment_count": int(comment_counts.get(row.id, 0)),
            "created_at": row.created_at,
            "origin_type": origin_type,
            "generated_at": contract_metadata.get("generated_at") if origin_type == "EVAL" else None,
            "modified_at": contract_metadata.get("modified_at") if origin_type == "SAMPLE" else None,
        })
    return result


@router.get("/gallery/context")
def gallery_context(
    db: DB,
    asset_id: str,
    project_id: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    decision: str | None = None,
    rating: int | None = Query(None, ge=1, le=5),
    model_id: str | None = None,
    dataset_id: str | None = None,
    eval_run_id: str | None = None,
    category: str | None = None,
    provider: str | None = None,
    include_dataset_assets: bool = False,
    sort: str = Query("origin_time_desc", pattern="^origin_time_(asc|desc)$"),
):
    query = _gallery_query(db, project_id, kind, q, decision, rating, model_id, dataset_id, eval_run_id, category, provider, include_dataset_assets, sort)
    order_clauses = tuple(query._order_by_clauses)
    indexed = query.order_by(None).add_columns(
        func.row_number().over(order_by=order_clauses).label("gallery_index"),
        func.count().over().label("gallery_total"),
    ).subquery()
    selected = db.execute(
        select(indexed.c.gallery_index, indexed.c.gallery_total).where(indexed.c.id == asset_id)
    ).first()
    if selected is None:
        raise HTTPException(status_code=404, detail="asset is not in the filtered gallery")
    selected_index = int(selected.gallery_index) - 1
    total = int(selected.gallery_total)
    window_ids = db.scalars(
        query.offset(max(0, selected_index - 4)).limit(9).with_only_columns(models.Asset.id)
    ).all()
    assets_by_id = {
        asset.id: asset
        for asset in db.scalars(select(models.Asset).options(selectinload(models.Asset.locations)).where(models.Asset.id.in_(window_ids))).all()
    }
    items = _gallery_rows(db, [assets_by_id[item_id] for item_id in window_ids if item_id in assets_by_id])
    current_index = next(index for index, item in enumerate(items) if item["id"] == asset_id)
    return {
        "index": selected_index,
        "total": total,
        "previous": items[current_index - 1] if selected_index > 0 and current_index > 0 else None,
        "current": items[current_index],
        "next": items[current_index + 1] if selected_index < total - 1 and current_index < len(items) - 1 else None,
        "items": items,
    }


@router.get("/gallery")
def gallery(
    db: DB,
    project_id: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    decision: str | None = None,
    rating: int | None = Query(None, ge=1, le=5),
    model_id: str | None = None,
    dataset_id: str | None = None,
    eval_run_id: str | None = None,
    category: str | None = None,
    provider: str | None = None,
    include_dataset_assets: bool = False,
    paginated: bool = False,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
    sort: str = Query("origin_time_desc", pattern="^origin_time_(asc|desc)$"),
):
    query = _gallery_query(db, project_id, kind, q, decision, rating, model_id, dataset_id, eval_run_id, category, provider, include_dataset_assets, sort)
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    rows = db.scalars(query.options(selectinload(models.Asset.locations)).offset(offset).limit(limit)).all()
    result = _gallery_rows(db, rows)
    return {"items": result, "total": total, "limit": limit, "offset": offset} if paginated else result


def _gallery_asset_metadata(db: Session, asset: models.Asset) -> dict:
    """Return one evidence-ordered provenance contract for a gallery asset."""
    metadata = dict(asset.metadata_ or {})
    relationship_keys = (
        "dataset_id", "dataset_name", "dataset_version_id", "dataset_version_name",
        "run_id", "run_name", "model_id", "model_name", "model_version_id",
        "model_version_name", "checkpoint_id", "checkpoint_name", "checkpoint_revision_id", "base_model",
        "provider", "asset_type", "origin_type",
    )
    source_rank = {
        "explicit": 0,
        "canonical": 1,
        "eval-sample": 2,
        "dataset-membership": 3,
        "deterministic-import": 4,
        "unknown": 99,
    }
    evidence = {
        key: ("explicit" if metadata.get(key) is not None else "unknown")
        for key in relationship_keys
    }

    def assign(key: str, value: Any, source: str) -> None:
        if value is None or value == "":
            return
        current = evidence.get(key, "unknown")
        if source_rank[source] <= source_rank[current]:
            metadata[key] = value
            evidence[key] = source

    # The asset row and its persisted metadata are canonical/explicit.  Never
    # infer an entity from the filename or substitute one entity for another.
    assign("asset_type", asset.kind.value if hasattr(asset.kind, "value") else asset.kind, "canonical")
    category = str(metadata.get("category", ""))
    category_origin = {"eval_output": "EVAL", "sample": "SAMPLE", "dataset_image": "DATASET"}.get(category)
    assign("origin_type", category_origin, "canonical")

    normalized_fal = normalize_fal_image_metadata(metadata)
    for key, value in normalized_fal.items():
        if key in relationship_keys:
            assign(key, value, "deterministic-import")
        else:
            metadata.setdefault(key, value)

    image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
    if image:
        metadata.update({key: value for key, value in {
            "width": image.width, "height": image.height, "color_mode": image.color_mode,
            "orientation": image.orientation, "exif": image.exif_summary or None,
        }.items() if value is not None})

    sample = db.scalar(select(models.Sample).where(models.Sample.asset_id == asset.id).order_by(models.Sample.created_at.desc()))
    if sample:
        run = db.get(models.TrainingRun, sample.run_id)
        filename_match = re.search(r"__(\d{6,})_(\d+)(?:\.[^.]+)?$", asset.name)
        source_step = int(filename_match.group(1)) if filename_match else sample.step
        sample_index = int(filename_match.group(2)) if filename_match else None
        assign("origin_type", "SAMPLE", "eval-sample")
        assign("run_id", sample.run_id, "eval-sample")
        assign("run_name", run.name if run else None, "eval-sample")
        assign("base_model", run.base_model if run else None, "eval-sample")
        assign("provider", (sample.generation_metadata or {}).get("provider"), "eval-sample")
        metadata.update({key: value for key, value in {
            "modified_at": sample.modified_at, "trainer": run.trainer if run else None,
            "step": source_step, "sample_index": sample_index, "prompt": sample.prompt,
            "seed": sample.seed, "generation_settings": sample.generation_metadata or None,
            "training_settings": run.normalized_config if run and run.normalized_config else None,
        }.items() if value is not None})
        if run and sample.checkpoint_id:
            model_rows = db.execute(
                select(models.Model.id, models.Model.name, models.ModelVersion.id, models.ModelVersion.name, models.ModelVersion.checkpoint_revision_id)
                .join(models.ModelVersion, models.ModelVersion.model_id == models.Model.id)
                .where(models.ModelVersion.checkpoint_id == sample.checkpoint_id)
                .distinct()
            ).all()
            if model_rows:
                model_id, model_name, version_id, version_name, checkpoint_revision_id = model_rows[0]
                assign("model_id", model_id, "eval-sample")
                assign("model_name", model_name, "eval-sample")
                assign("checkpoint_name", f"step {db.get(models.Checkpoint, sample.checkpoint_id).step}", "eval-sample")
                assign("model_version_id", version_id, "eval-sample")
                assign("model_version_name", version_name, "eval-sample")
                assign("checkpoint_id", sample.checkpoint_id, "eval-sample")
                metadata["checkpoint_revision_id"] = checkpoint_revision_id
                metadata["models"] = [{"id": row[0], "name": row[1], "model_version_id": row[2], "model_version_name": row[3], "checkpoint_revision_id": row[4]} for row in model_rows]
            if run.dataset_version_id:
                version = db.get(models.DatasetVersion, run.dataset_version_id)
                dataset = db.get(models.Dataset, version.dataset_id) if version else None
                assign("dataset_version_id", run.dataset_version_id, "eval-sample")
                assign("dataset_version_name", version.name if version else None, "eval-sample")
                assign("dataset_id", dataset.id if dataset else None, "eval-sample")
                assign("dataset_name", dataset.name if dataset else None, "eval-sample")

    output = db.scalar(select(models.EvalOutput).where(models.EvalOutput.asset_id == asset.id).order_by(models.EvalOutput.created_at.desc()))
    if output:
        eval_run = db.get(models.EvalRun, output.eval_run_id)
        definition = db.get(models.EvalDefinition, eval_run.definition_id) if eval_run else None
        provider_metadata = dict(output.provider_metadata or {})
        grid_metadata = dict(provider_metadata.get("grid_metadata") or {})
        version_id = str(
            (definition.model_version_id if definition else None)
            or grid_metadata.get("model_version_id")
            or metadata.get("model_version_id")
            or ""
        )
        version = db.get(models.ModelVersion, version_id) if version_id else None
        model = db.get(models.Model, version.model_id) if version else None
        prompt = db.get(models.Prompt, output.prompt_id) if output.prompt_id else None
        checkpoint = db.get(models.Checkpoint, version.checkpoint_id) if version else None
        training_run = db.get(models.TrainingRun, checkpoint.run_id) if checkpoint else None
        merge_operation = db.scalar(
            select(models.MergeOperation).where(
                models.MergeOperation.output_checkpoint_revision_id == version.checkpoint_revision_id
            )
        ) if version and version.checkpoint_revision_id else None
        dataset_version = db.get(models.DatasetVersion, training_run.dataset_version_id) if training_run and training_run.dataset_version_id and not merge_operation else None
        dataset = db.get(models.Dataset, dataset_version.dataset_id) if dataset_version else None
        request_input = metadata.get("fal_request_input")
        if not isinstance(request_input, dict):
            request_input = output.provider_metadata.get("_request_input") if isinstance(output.provider_metadata, dict) else None
        assign("origin_type", "EVAL", "eval-sample")
        assign("eval_run_id", output.eval_run_id, "eval-sample")
        assign("model_id", model.id if model else None, "eval-sample")
        assign("model_name", model.name if model else None, "eval-sample")
        assign("model_version_id", version.id if version else None, "eval-sample")
        assign("model_version_name", version.name if version else None, "eval-sample")
        assign("base_model", version.base_model if version else None, "eval-sample")
        if merge_operation:
            metadata.pop("run_id", None)
            metadata.pop("run_name", None)
            evidence["run_id"] = "unknown"
            evidence["run_name"] = "unknown"
            metadata.pop("checkpoint_step", None)
            metadata["checkpoint_name"] = f"merged output · rank {merge_operation.output_rank}"
            metadata.update({
                "provenance_kind": "merge",
                "merge_operation_id": merge_operation.id,
                "merge_operator": merge_operation.operator,
                "merge_notation": merge_operation.compact_notation,
                "merge_recipe": dict(merge_operation.recipe or {}),
                "merge_schema_version": merge_operation.schema_version,
            })
        else:
            assign("run_id", training_run.id if training_run else None, "eval-sample")
            assign("run_name", training_run.name if training_run else None, "eval-sample")
            assign("dataset_id", dataset.id if dataset else None, "eval-sample")
            assign("dataset_name", dataset.name if dataset else None, "eval-sample")
            assign("dataset_version_id", dataset_version.id if dataset_version else None, "eval-sample")
            assign("dataset_version_name", dataset_version.name if dataset_version else None, "eval-sample")
        assign("checkpoint_id", checkpoint.id if checkpoint else None, "eval-sample")
        assign("checkpoint_name", f"step {checkpoint.step}" if checkpoint else None, "eval-sample")
        assign("checkpoint_revision_id", version.checkpoint_revision_id if version else None, "eval-sample")
        provider = output.provider_metadata.get("provider") if isinstance(output.provider_metadata, dict) else None
        workflow = output.provider_metadata.get("workflow") if isinstance(output.provider_metadata, dict) else None
        assign("provider", provider, "eval-sample")
        metadata.update({key: value for key, value in {
            "generated_by": provider,
            "workflow": workflow,
            "workflow_url": workflow.get("url") if isinstance(workflow, dict) else None,
            "workflow_asset_id": workflow.get("asset_id") if isinstance(workflow, dict) else None,
            "generated_at": output.generated_at, "eval_name": definition.name if definition else None,
            "endpoint": definition.endpoint if definition else None,
            "prompt": prompt.text if prompt else None, "seed": output.seed,
            "generation_settings": request_input or (eval_run.parameters_snapshot if eval_run and eval_run.parameters_snapshot else (definition.parameters if definition else None)),
            "provider_metadata": output.provider_metadata or None,
            "model_attribution": "explicit" if model else None,
        }.items() if value is not None})

    item = db.scalar(select(models.DatasetItem).where(models.DatasetItem.asset_id == asset.id).order_by(models.DatasetItem.created_at.desc()))
    if item:
        version = db.get(models.DatasetVersion, item.dataset_version_id)
        dataset = db.get(models.Dataset, version.dataset_id) if version else None
        assign("origin_type", "DATASET", "dataset-membership")
        assign("dataset_id", dataset.id if dataset else None, "dataset-membership")
        assign("dataset_name", dataset.name if dataset else None, "dataset-membership")
        assign("dataset_version_id", version.id if version else None, "dataset-membership")
        assign("dataset_version_name", version.name if version else None, "dataset-membership")
        metadata.update({key: value for key, value in {
            "caption": item.caption, "caption_format": item.caption_format,
            "included": item.included, "tags": item.tags or None,
        }.items() if value is not None})

    remote = next((location for location in asset.locations if location.provider != "local"), asset.locations[0] if asset.locations else None)
    if remote:
        metadata["storage"] = {key: value for key, value in {
            "provider": remote.provider, "bucket": remote.bucket, "object_key": remote.object_key,
            "uri": remote.uri, "size": remote.size, "modified_at": remote.modified_at,
            "hydration_state": remote.hydration_state,
        }.items() if value is not None}
        assign("provider", remote.provider, "deterministic-import")
    metadata.setdefault("provenance_kind", asset.provenance_kind)
    for key in relationship_keys:
        evidence.setdefault(key, "unknown")
    metadata["evidence"] = evidence
    metadata["metadata_contract_version"] = 1
    metadata["relationships"] = {
        "dataset": {"id": metadata.get("dataset_id"), "name": metadata.get("dataset_name"), "evidence": evidence["dataset_id"]},
        "training_run": {"id": None, "name": "Not applicable — merge output", "evidence": "canonical"} if metadata.get("merge_operation_id") else {"id": metadata.get("run_id"), "name": metadata.get("run_name"), "evidence": evidence["run_id"]},
        "model": {"id": metadata.get("model_id"), "name": metadata.get("model_name"), "evidence": evidence["model_id"]},
        "model_version": {"id": metadata.get("model_version_id"), "name": metadata.get("model_version_name"), "evidence": evidence["model_version_id"]},
        "checkpoint": {"id": metadata.get("checkpoint_id"), "name": metadata.get("checkpoint_name"), "evidence": evidence["checkpoint_id"]},
        "merge_operation": {"id": metadata.get("merge_operation_id"), "name": metadata.get("merge_notation"), "evidence": "canonical" if metadata.get("merge_operation_id") else "unknown"},
        "base_model": {"id": None, "name": metadata.get("base_model"), "evidence": evidence["base_model"]},
        "asset_type": {"id": None, "name": metadata.get("asset_type"), "evidence": evidence["asset_type"]},
        "provider": {"id": None, "name": metadata.get("provider"), "evidence": evidence["provider"]},
    }
    return metadata


@router.post("/assets", response_model=schemas.AssetOut, status_code=status.HTTP_201_CREATED, include_in_schema=False)
def create_asset(body: schemas.AssetCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    workspace = current_workspace(db)
    data = body.model_dump(exclude={"location", "metadata"})
    asset = models.Asset(workspace_id=workspace.id, metadata_=body.metadata, **data)
    db.add(asset)
    db.flush()
    if body.location:
        location_data = body.location.model_dump()
        source, key = _resolve_s3_source(db, workspace.id, body.location)
        if source is not None:
            location_data.update(
                workspace_id=workspace.id,
                source_id=source.id,
                source_revision_fingerprint=source.identity_fingerprint,
                bucket=source.bucket,
            )
            if key is not None:
                location_data["object_key"] = key
            if body.sha256 is not None and location_data.get("size") is not None:
                StorageRepository(db, workspace.id).attach_verified_remote_location(
                    asset_id=asset.id,
                    source_id=source.id,
                    object_key=key or str(location_data["object_key"]),
                    etag=location_data.get("etag"),
                    size=int(location_data["size"]),
                    sha256=body.sha256,
                    mime_type=asset.mime_type or "application/octet-stream",
                )
            else:
                db.add(models.AssetLocation(asset_id=asset.id, **location_data))
        else:
            db.add(models.AssetLocation(asset_id=asset.id, **location_data))
    record_activity(db, action="asset.created", subject_type="asset", subject_id=asset.id, profile_id=profile_for(db, x_profile_id).id, project_id=asset.project_id)
    db.commit()
    return db.scalar(select(models.Asset).options(selectinload(models.Asset.locations)).where(models.Asset.id == asset.id))


@router.get("/assets/{asset_id}", response_model=schemas.AssetOut)
def asset(asset_id: str, db: DB):
    asset = db.scalar(select(models.Asset).options(selectinload(models.Asset.locations)).where(models.Asset.id == asset_id, models.Asset.workspace_id == current_workspace(db).id))
    if asset is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.get("/assets/{asset_id}/locations", response_model=list[schemas.AssetLocationOut], include_in_schema=False)
def asset_locations(asset_id: str, db: DB):
    workspace_object(db, models.Asset, asset_id)
    return db.scalars(select(models.AssetLocation).where(models.AssetLocation.asset_id == asset_id)).all()


@router.get("/assets/{asset_id}/metadata", include_in_schema=False)
def asset_metadata(asset_id: str, db: DB):
    asset = workspace_object(db, models.Asset, asset_id)
    image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset_id))
    return {
        "asset_id": asset.id,
        "asset_revision_id": asset.id,
        "metadata": _gallery_asset_metadata(db, asset),
        "image": None if image is None else {column.name: getattr(image, column.key) for column in image.__table__.columns},
    }


@router.get("/assets/{asset_id}/context")
def asset_context(asset_id: str, db: DB):
    asset = workspace_object(db, models.Asset, asset_id)
    image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
    reviews = db.scalars(select(models.Review).where(models.Review.subject_type == "asset", models.Review.subject_id == asset.id).order_by(models.Review.created_at.desc())).all()
    comments = db.scalars(select(models.Comment).where(models.Comment.subject_type == "asset", models.Comment.subject_id == asset.id).order_by(models.Comment.created_at)).all()
    profile_ids = {row.profile_id for row in [*reviews, *comments]}
    profiles = {row.id: row.display_name for row in db.scalars(select(models.UserProfile).where(models.UserProfile.id.in_(profile_ids))).all()} if profile_ids else {}
    return {
        "asset_revision_id": asset.id,
        "asset": schemas.AssetOut.model_validate(asset).model_dump(mode="json"),
        "metadata": _gallery_asset_metadata(db, asset),
        "image": None if image is None else {column.name: getattr(image, column.key) for column in image.__table__.columns},
        "reviews": [{**{column.name: getattr(row, column.key) for column in row.__table__.columns}, "profile_name": profiles.get(row.profile_id)} for row in reviews],
        "comments": [{**{column.name: getattr(row, column.key) for column in row.__table__.columns}, "profile_name": profiles.get(row.profile_id)} for row in comments],
    }


def _local_asset_path(db: Session, asset_id: str) -> tuple[models.Asset, Path]:
    asset = workspace_object(db, models.Asset, asset_id)
    location = db.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == asset.id, models.AssetLocation.provider == "local", models.AssetLocation.hydration_state == "hydrated").order_by(models.AssetLocation.created_at.desc()))
    if location is None:
        raise HTTPException(status_code=409, detail="asset is remote-only; hydrate it before requesting content")
    path = Path(location.uri).expanduser().resolve()
    settings = get_settings()
    roots = (settings.asset_root.resolve(), settings.cache_root.resolve())
    if not any(path == root or root in path.parents for root in roots):
        raise HTTPException(status_code=403, detail="asset location is outside configured storage roots")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="hydrated asset content is missing")
    return asset, path


@router.get("/assets/{asset_id}/content", include_in_schema=False)
def asset_content(asset_id: str, db: DB):
    asset, path = _local_asset_path(db, asset_id)
    return FileResponse(path, media_type=asset.mime_type or "application/octet-stream")


@router.get("/assets/{asset_id}/thumbnail", include_in_schema=False)
def asset_thumbnail(asset_id: str, db: DB, size: int = Query(512)):
    asset, source = _local_asset_path(db, asset_id)
    if asset.kind != models.AssetKind.image:
        raise HTTPException(status_code=415, detail="thumbnails are only available for image assets")
    try:
        cache = AssetCache(CacheSettings(root=get_settings().cache_root.resolve()))
        path = cache.thumbnail(asset.id, source, size)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=415, detail=f"thumbnail generation failed: {exc}") from exc
    return FileResponse(path, media_type="image/webp")


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(asset_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None, report: bool = False, delete_content: bool = False):
    asset = get_or_404(db, models.Asset, asset_id)
    project_id = asset.project_id
    if db.scalar(select(models.DatasetItem.id).where(models.DatasetItem.asset_id == asset_id).limit(1)):
        raise HTTPException(status_code=409, detail="Dataset source images cannot be deleted from Gallery; remove them through dataset versioning.")
    if db.scalar(select(models.Checkpoint.id).where(models.Checkpoint.asset_id == asset_id).limit(1)):
        raise HTTPException(status_code=409, detail="Model checkpoint artifacts cannot be deleted from Gallery.")
    locations = [{"provider": item.provider, "uri": item.uri} for item in asset.locations]
    location_ids = [item.id for item in asset.locations]
    if location_ids:
        referencing_assets = db.scalars(
            select(models.Asset).where(
                or_(
                    models.Asset.preferred_location_id.in_(location_ids),
                    models.Asset.origin_location_id.in_(location_ids),
                )
            )
        ).all()
        for referencing_asset in referencing_assets:
            if referencing_asset.preferred_location_id in location_ids:
                referencing_asset.preferred_location_id = None
            if referencing_asset.origin_location_id in location_ids:
                referencing_asset.origin_location_id = None
        db.flush()
    db.query(models.Sample).filter(models.Sample.asset_id == asset_id).delete(synchronize_session=False)
    db.query(models.EvalOutput).filter(models.EvalOutput.asset_id == asset_id).delete(synchronize_session=False)
    db.query(models.Review).filter(models.Review.subject_type == "asset", models.Review.subject_id == asset_id).delete(synchronize_session=False)
    db.query(models.Comment).filter(models.Comment.subject_type == "asset", models.Comment.subject_id == asset_id).delete(synchronize_session=False)
    db.query(models.Note).filter(models.Note.subject_type == "asset", models.Note.subject_id == asset_id).delete(synchronize_session=False)
    db.delete(asset)
    record_activity(db, action="asset.deleted", subject_type="asset", subject_id=asset_id, profile_id=profile_for(db, x_profile_id).id, project_id=project_id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Asset metadata is still referenced and could not be deleted safely.") from exc
    storage = []
    for location in locations:
        if not delete_content:
            storage.append({**location, "status": "retained", "detail": "Content deletion was not requested."})
            continue
        if location["provider"] != "local":
            storage.append({**location, "status": "retained", "detail": "Remote object deletion is not supported by Gallery; metadata was removed."})
            continue
        try:
            path = Path(location["uri"]).expanduser().resolve()
            roots = (get_settings().asset_root.resolve(), get_settings().cache_root.resolve())
            if not any(path == root or root in path.parents for root in roots):
                raise PermissionError("path is outside configured storage roots")
            if path.exists(): path.unlink()
            storage.append({**location, "status": "deleted"})
        except Exception as exc:
            storage.append({**location, "status": "failed", "detail": str(exc)})
    if report:
        result = {"asset_id": asset_id, "metadata": "deleted", "storage": storage}
        return JSONResponse(result, status_code=207 if any(item["status"] == "failed" for item in storage) else 200)
    return Response(status_code=204)
