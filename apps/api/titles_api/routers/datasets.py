from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import active_profile, get_or_404, record_activity

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def _dump(row) -> dict[str, Any]:
    return {column.name: getattr(row, column.key) for column in row.__table__.columns}


def _asset_fields(db: Session, asset_id: str) -> dict[str, Any]:
    asset = db.get(models.Asset, asset_id)
    image = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset_id))
    locations = db.scalars(select(models.AssetLocation).where(models.AssetLocation.asset_id == asset_id)).all()
    return {
        "asset_revision_id": asset_id,
        "filename": asset.name if asset else None,
        "mime_type": asset.mime_type if asset else None,
        "asset_metadata": dict(asset.metadata_ or {}) if asset else {},
        "width": image.width if image else None,
        "height": image.height if image else None,
        "origin_type": "DATASET",
        "generated_at": None,
        "modified_at": None,
        "availability": "available" if locations else "missing",
        "availability_error": None if locations else "No storage object is associated with this dataset asset.",
    }


@router.get("/datasets")
def list_datasets(db: DB, project_id: str | None = None):
    query = select(models.Dataset).order_by(models.Dataset.updated_at.desc())
    if project_id:
        query = query.where(models.Dataset.project_id == project_id)
    rows = []
    for row in db.scalars(query).all():
        project = db.get(models.Project, row.project_id)
        version = db.scalar(select(models.DatasetVersion).where(models.DatasetVersion.dataset_id == row.id).order_by(models.DatasetVersion.version_number.desc()))
        rows.append({
            **_dump(row),
            "project_name": project.title if project else None,
            "current_version": version.version_number if version else None,
            "current_version_id": version.id if version else None,
            "item_count": int(db.scalar(select(func.count()).select_from(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)) or 0) if version else 0,
        })
    return rows


@router.post("/datasets", status_code=status.HTTP_201_CREATED)
def create_dataset(body: schemas.DatasetCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    project = get_or_404(db, models.Project, body.project_id)
    actor = active_profile(db, x_profile_id)
    dataset = models.Dataset(project_id=project.id, name=body.name, description=body.description)
    db.add(dataset)
    db.flush()
    version = models.DatasetVersion(dataset_id=dataset.id, version_number=1, name="Initial import", source_uri=body.source_uri, caption_format=body.caption_format, trigger_words=body.trigger_words, published_by_profile_id=actor.id)
    db.add(version)
    db.flush()
    for item in body.items:
        get_or_404(db, models.Asset, item.asset_id)
        db.add(models.DatasetItem(dataset_version_id=version.id, **item.model_dump()))
    record_activity(db, action="dataset.created", subject_type="dataset", subject_id=dataset.id, profile_id=actor.id, project_id=project.id, details={"version_id": version.id, "items": len(body.items)})
    db.commit()
    return {**_dump(dataset), "current_version_id": version.id}


@router.get("/datasets/{dataset_id}")
def dataset(dataset_id: str, db: DB):
    dataset = get_or_404(db, models.Dataset, dataset_id)
    versions = db.scalars(select(models.DatasetVersion).where(models.DatasetVersion.dataset_id == dataset.id).order_by(models.DatasetVersion.version_number.desc())).all()
    project = db.get(models.Project, dataset.project_id)
    version_rows = [{**_dump(v), "item_count": int(db.scalar(select(func.count()).select_from(models.DatasetItem).where(models.DatasetItem.dataset_version_id == v.id)) or 0)} for v in versions]
    current = versions[0] if versions else None
    version_ids = [version.id for version in versions]
    runs = db.scalars(select(models.TrainingRun).where(models.TrainingRun.dataset_version_id.in_(version_ids)).order_by(models.TrainingRun.created_at.desc())).all() if version_ids else []
    return {
        **_dump(dataset),
        "project_name": project.title if project else None,
        "current_version": current.version_number if current else None,
        "current_version_id": current.id if current else None,
        "source_uri": current.source_uri if current else None,
        "trigger_words": current.trigger_words if current else [],
        "versions": version_rows,
        "training_runs": [{"id": run.id, "name": run.name, "status": run.status, "dataset_version_id": run.dataset_version_id} for run in runs],
        "empty_reason": "No source image objects were imported for this dataset." if current and not version_rows[0]["item_count"] else None,
    }


@router.get("/dataset-versions/{version_id}/items")
def version_items(version_id: str, db: DB, included: bool | None = None, limit: int = Query(200, le=1000), offset: int = 0):
    get_or_404(db, models.DatasetVersion, version_id)
    query = select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version_id).order_by(models.DatasetItem.position)
    if included is not None:
        query = query.where(models.DatasetItem.included == included)
    rows = []
    for item in db.scalars(query.offset(offset).limit(limit)).all():
        rows.append(
            {
                **_dump(item),
                **_asset_fields(db, item.asset_id),
            }
        )
    return rows


@router.get("/dataset-versions/{version_id}")
def dataset_version(version_id: str, db: DB):
    version = get_or_404(db, models.DatasetVersion, version_id)
    dataset = get_or_404(db, models.Dataset, version.dataset_id)
    return {
        **_dump(version),
        "dataset_name": dataset.name,
        "project_id": dataset.project_id,
        "item_count": int(db.scalar(select(func.count()).select_from(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)) or 0),
    }


@router.post("/datasets/{dataset_id}/drafts", status_code=status.HTTP_201_CREATED)
def create_draft(dataset_id: str, body: schemas.DraftCreate, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    dataset = get_or_404(db, models.Dataset, dataset_id)
    base = get_or_404(db, models.DatasetVersion, body.base_version_id)
    if base.dataset_id != dataset.id:
        raise HTTPException(status_code=400, detail="base version does not belong to dataset")
    actor = active_profile(db, x_profile_id)
    draft = models.DatasetDraft(dataset_id=dataset.id, base_version_id=base.id, created_by_profile_id=actor.id)
    db.add(draft)
    db.flush()
    for item in db.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == base.id)):
        db.add(models.DatasetDraftItem(draft_id=draft.id, source_item_id=item.id, asset_id=item.asset_id, caption=item.caption, caption_format=item.caption_format, included=item.included, tags=item.tags, position=item.position))
    record_activity(db, action="dataset_draft.created", subject_type="dataset_draft", subject_id=draft.id, profile_id=actor.id, project_id=dataset.project_id)
    db.commit()
    return _dump(draft)


@router.get("/dataset-drafts/{draft_id}")
def draft(draft_id: str, db: DB):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    items = db.scalars(select(models.DatasetDraftItem).where(models.DatasetDraftItem.draft_id == draft.id).order_by(models.DatasetDraftItem.position)).all()
    rows = []
    for item in items:
        rows.append(
            {
                **_dump(item),
                **_asset_fields(db, item.asset_id),
            }
        )
    return {**_dump(draft), "items": rows}


@router.patch("/dataset-drafts/{draft_id}/items/{item_id}")
def patch_draft_item(draft_id: str, item_id: str, body: schemas.DraftItemPatch, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    item = get_or_404(db, models.DatasetDraftItem, item_id)
    if item.draft_id != draft.id:
        raise HTTPException(status_code=404, detail="draft item not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    record_activity(db, action="caption.updated", subject_type="dataset_draft_item", subject_id=item.id, profile_id=active_profile(db, x_profile_id).id, project_id=dataset.project_id)
    db.commit()
    return _dump(item)


def _apply(operation: schemas.CaptionOperationRequest, items: list[models.DatasetDraftItem], mutate: bool) -> dict[str, Any]:
    changes = []
    for item in items:
        before = item.caption
        after = before
        if operation.operation == "replace":
            after = before.replace(str(operation.parameters["find"]), str(operation.parameters["replace"]))
        elif operation.operation == "add_word":
            word = str(operation.parameters["word"])
            if word and word not in before:
                after = f"{before.rstrip(', ')}{', ' if before else ''}{word}"
        elif operation.operation == "remove_word":
            word = str(operation.parameters["word"])
            after = ", ".join(part.strip() for part in before.split(",") if part.strip() != word)
        elif operation.operation == "set_included":
            if item.included != bool(operation.parameters["included"]):
                changes.append({"item_id": item.id, "before": item.included, "after": bool(operation.parameters["included"])})
                if mutate:
                    item.included = bool(operation.parameters["included"])
            continue
        if after != before:
            changes.append({"item_id": item.id, "before": before, "after": after})
            if mutate:
                item.caption = after
    return {"matched": len(items), "changed": len(changes), "changes": changes[:100], "truncated": len(changes) > 100}


def _operation(draft_id: str, body: schemas.CaptionOperationRequest, db: Session, mutate: bool, profile_id: str | None):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    query = select(models.DatasetDraftItem).where(models.DatasetDraftItem.draft_id == draft.id)
    if body.item_ids:
        query = query.where(models.DatasetDraftItem.id.in_(body.item_ids))
    if body.tag is not None:
        # SQLite JSON array membership without relying on JSON1 table functions.
        query = query.where(models.DatasetDraftItem.tags.contains(body.tag))
    if body.included is not None:
        query = query.where(models.DatasetDraftItem.included == body.included)
    if body.caption_format is not None:
        query = query.where(models.DatasetDraftItem.caption_format == body.caption_format)
    items = list(db.scalars(query).all())
    preview = _apply(body, items, mutate)
    if mutate:
        actor = active_profile(db, profile_id)
        db.add(models.CaptionOperation(draft_id=draft.id, profile_id=actor.id, operation=body.operation, parameters=body.parameters, scope=body.model_dump(include={"item_ids", "tag", "included", "caption_format", "all"}), before_count=len(items), after_count=preview["changed"], preview=preview))
        dataset = get_or_404(db, models.Dataset, draft.dataset_id)
        record_activity(db, action="caption.batch_applied", subject_type="dataset_draft", subject_id=draft.id, profile_id=actor.id, project_id=dataset.project_id, details={"operation": body.operation, "changed": preview["changed"]})
        db.commit()
    return preview


@router.post("/dataset-drafts/{draft_id}/operations/preview")
def preview_operation(draft_id: str, body: schemas.CaptionOperationRequest, db: DB):
    return _operation(draft_id, body, db, False, None)


@router.post("/dataset-drafts/{draft_id}/operations")
def apply_operation(draft_id: str, body: schemas.CaptionOperationRequest, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    return _operation(draft_id, body, db, True, x_profile_id)


@router.post("/dataset-drafts/{draft_id}/publish", status_code=status.HTTP_201_CREATED)
def publish(draft_id: str, body: schemas.DraftPublish, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    base = get_or_404(db, models.DatasetVersion, draft.base_version_id)
    next_number = (db.scalar(select(func.max(models.DatasetVersion.version_number)).where(models.DatasetVersion.dataset_id == dataset.id)) or 0) + 1
    actor = active_profile(db, x_profile_id)
    version = models.DatasetVersion(dataset_id=dataset.id, version_number=next_number, name=body.name, source_uri=base.source_uri, caption_format=base.caption_format, trigger_words=base.trigger_words, published_by_profile_id=actor.id)
    db.add(version)
    db.flush()
    for item in db.scalars(select(models.DatasetDraftItem).where(models.DatasetDraftItem.draft_id == draft.id)):
        db.add(models.DatasetItem(dataset_version_id=version.id, asset_id=item.asset_id, caption=item.caption, caption_format=item.caption_format, included=item.included, tags=item.tags, position=item.position))
    draft.state = "published"
    record_activity(db, action="dataset_version.published", subject_type="dataset_version", subject_id=version.id, profile_id=actor.id, project_id=dataset.project_id, details={"draft_id": draft.id, "version": next_number})
    db.commit()
    return _dump(version)


@router.get("/datasets/{dataset_id}/versions")
def dataset_versions(dataset_id: str, db: DB):
    get_or_404(db, models.Dataset, dataset_id)
    return [_dump(v) for v in db.scalars(select(models.DatasetVersion).where(models.DatasetVersion.dataset_id == dataset_id).order_by(models.DatasetVersion.version_number.desc())).all()]


@router.delete("/dataset-drafts/{draft_id}", status_code=204)
def discard_draft(draft_id: str, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="only active drafts can be discarded")
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    draft.state = "discarded"
    record_activity(db, action="dataset_draft.discarded", subject_type="dataset_draft", subject_id=draft.id, profile_id=active_profile(db, x_profile_id).id, project_id=dataset.project_id)
    db.commit()
