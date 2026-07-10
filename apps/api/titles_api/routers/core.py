from typing import Annotated

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import models, schemas
from ..database import get_db
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..asset_cache import AssetCache
from ..integrations.config import CacheSettings
from ..settings import get_settings

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def profile_for(db: Session, profile_id: str | None) -> models.UserProfile:
    return active_profile(db, profile_id)


@router.get("/workspaces", response_model=list[schemas.WorkspaceOut])
def workspaces(db: DB):
    return db.scalars(select(models.Workspace).order_by(models.Workspace.name)).all()


@router.get("/profiles", response_model=list[schemas.ProfileOut])
def profiles(db: DB, include_archived: bool = False):
    query = select(models.UserProfile).order_by(models.UserProfile.display_name)
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
    return get_or_404(db, models.UserProfile, profile_id)


@router.patch("/profiles/{profile_id}", response_model=schemas.ProfileOut)
def update_profile(profile_id: str, body: schemas.ProfileUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    profile = get_or_404(db, models.UserProfile, profile_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(profile, key, value)
    record_activity(db, action="profile.updated", subject_type="profile", subject_id=profile.id, profile_id=profile_for(db, x_profile_id).id)
    db.commit()
    return profile


@router.put("/profiles/{profile_id}/active", response_model=schemas.ProfileOut)
def use_profile(profile_id: str, db: DB):
    profile = get_or_404(db, models.UserProfile, profile_id)
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
    profile = get_or_404(db, models.UserProfile, profile_id)
    actor = profile_for(db, x_profile_id)
    active_count = int(db.scalar(select(func.count()).select_from(models.UserProfile).where(models.UserProfile.is_active.is_(True))) or 0)
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
def projects(db: DB, state: str | None = None):
    query = select(models.Project).order_by(models.Project.updated_at.desc())
    if state:
        query = query.where(models.Project.state == state)
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
    return get_or_404(db, models.Project, project_id)


@router.patch("/projects/{project_id}", response_model=schemas.ProjectOut)
def update_project(project_id: str, body: schemas.ProjectUpdate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    project = get_or_404(db, models.Project, project_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    record_activity(db, action="project.updated", subject_type="project", subject_id=project.id, profile_id=profile_for(db, x_profile_id).id, project_id=project.id)
    db.commit()
    return project


@router.get("/assets", response_model=list[schemas.AssetOut])
def assets(db: DB, project_id: str | None = None, kind: str | None = None, q: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    query = select(models.Asset).options(selectinload(models.Asset.locations)).order_by(models.Asset.created_at.desc())
    if project_id:
        query = query.where(models.Asset.project_id == project_id)
    if kind:
        query = query.where(models.Asset.kind == kind)
    if q:
        query = query.where(models.Asset.name.contains(q))
    return db.scalars(query.offset(offset).limit(limit)).all()


@router.get("/gallery")
def gallery(
    db: DB,
    project_id: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    decision: str | None = None,
    rating: int | None = Query(None, ge=1, le=5),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    query = select(models.Asset).options(selectinload(models.Asset.locations)).order_by(models.Asset.created_at.desc())
    if project_id:
        query = query.where(models.Asset.project_id == project_id)
    if kind:
        query = query.where(models.Asset.kind == kind)
    if q:
        query = query.where(models.Asset.name.contains(q))
    if decision or rating is not None:
        reviewed_assets = select(models.Review.subject_id).where(models.Review.subject_type == "asset")
        if decision:
            reviewed_assets = reviewed_assets.where(models.Review.decision == decision)
        if rating is not None:
            reviewed_assets = reviewed_assets.where(models.Review.rating == rating)
        query = query.where(models.Asset.id.in_(reviewed_assets))
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    result = []
    for row in rows:
        review = db.scalar(select(models.Review).where(models.Review.subject_type == "asset", models.Review.subject_id == row.id).order_by(models.Review.created_at.desc()))
        result.append({
            "id": row.id,
            "project_id": row.project_id,
            "kind": row.kind.value if hasattr(row.kind, "value") else row.kind,
            "name": row.name,
            "mime_type": row.mime_type,
            "sha256": row.sha256,
            "metadata": row.metadata_,
            "locations": [schemas.AssetLocationOut.model_validate(location).model_dump(mode="json") for location in row.locations],
            "rating": review.rating if review else None,
            "decision": review.decision if review else None,
            "review_count": int(db.scalar(select(func.count()).select_from(models.Review).where(models.Review.subject_type == "asset", models.Review.subject_id == row.id)) or 0),
            "comment_count": int(db.scalar(select(func.count()).select_from(models.Comment).where(models.Comment.subject_type == "asset", models.Comment.subject_id == row.id)) or 0),
            "created_at": row.created_at,
        })
    return result


@router.post("/assets", response_model=schemas.AssetOut, status_code=status.HTTP_201_CREATED)
def create_asset(body: schemas.AssetCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    data = body.model_dump(exclude={"location", "metadata"})
    asset = models.Asset(workspace_id=current_workspace(db).id, metadata_=body.metadata, **data)
    db.add(asset)
    db.flush()
    if body.location:
        db.add(models.AssetLocation(asset_id=asset.id, **body.location.model_dump()))
    record_activity(db, action="asset.created", subject_type="asset", subject_id=asset.id, profile_id=profile_for(db, x_profile_id).id, project_id=asset.project_id)
    db.commit()
    return db.scalar(select(models.Asset).options(selectinload(models.Asset.locations)).where(models.Asset.id == asset.id))


@router.get("/assets/{asset_id}", response_model=schemas.AssetOut)
def asset(asset_id: str, db: DB):
    asset = db.scalar(select(models.Asset).options(selectinload(models.Asset.locations)).where(models.Asset.id == asset_id))
    if asset is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.get("/assets/{asset_id}/locations", response_model=list[schemas.AssetLocationOut])
def asset_locations(asset_id: str, db: DB):
    get_or_404(db, models.Asset, asset_id)
    return db.scalars(select(models.AssetLocation).where(models.AssetLocation.asset_id == asset_id)).all()


@router.get("/assets/{asset_id}/metadata")
def asset_metadata(asset_id: str, db: DB):
    asset = get_or_404(db, models.Asset, asset_id)
    image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset_id))
    return {"asset_id": asset.id, "metadata": asset.metadata_, "image": None if image is None else {column.name: getattr(image, column.key) for column in image.__table__.columns}}


@router.get("/assets/{asset_id}/context")
def asset_context(asset_id: str, db: DB):
    asset = get_or_404(db, models.Asset, asset_id)
    image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
    reviews = db.scalars(select(models.Review).where(models.Review.subject_type == "asset", models.Review.subject_id == asset.id).order_by(models.Review.created_at.desc())).all()
    comments = db.scalars(select(models.Comment).where(models.Comment.subject_type == "asset", models.Comment.subject_id == asset.id).order_by(models.Comment.created_at)).all()
    profile_ids = {row.profile_id for row in [*reviews, *comments]}
    profiles = {row.id: row.display_name for row in db.scalars(select(models.UserProfile).where(models.UserProfile.id.in_(profile_ids))).all()} if profile_ids else {}
    return {
        "asset": schemas.AssetOut.model_validate(asset).model_dump(mode="json"),
        "metadata": asset.metadata_,
        "image": None if image is None else {column.name: getattr(image, column.key) for column in image.__table__.columns},
        "reviews": [{**{column.name: getattr(row, column.key) for column in row.__table__.columns}, "profile_name": profiles.get(row.profile_id)} for row in reviews],
        "comments": [{**{column.name: getattr(row, column.key) for column in row.__table__.columns}, "profile_name": profiles.get(row.profile_id)} for row in comments],
    }


def _local_asset_path(db: Session, asset_id: str) -> tuple[models.Asset, Path]:
    asset = get_or_404(db, models.Asset, asset_id)
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


@router.get("/assets/{asset_id}/content")
def asset_content(asset_id: str, db: DB):
    asset, path = _local_asset_path(db, asset_id)
    return FileResponse(path, media_type=asset.mime_type or "application/octet-stream")


@router.get("/assets/{asset_id}/thumbnail")
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
def delete_asset(asset_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    asset = get_or_404(db, models.Asset, asset_id)
    project_id = asset.project_id
    db.delete(asset)
    record_activity(db, action="asset.deleted", subject_type="asset", subject_id=asset_id, profile_id=profile_for(db, x_profile_id).id, project_id=project_id)
    db.commit()
    return Response(status_code=204)
