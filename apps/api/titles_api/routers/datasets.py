import hashlib
import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..dataset_versions import content_digest
from ..database import get_db
from ..integrations.llm import LLMConfigurationError, LLMResponseError, PromptGenerator
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..settings import get_settings

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

def _dump_draft(db: Session, draft: models.DatasetDraft) -> dict[str, Any]:
    items = db.scalars(
        select(models.DatasetDraftItem)
        .where(models.DatasetDraftItem.draft_id == draft.id)
        .order_by(models.DatasetDraftItem.position)
    ).all()
    return {
        **_dump(draft),
        "items": [{**_dump(item), **_asset_fields(db, item.asset_id)} for item in items],
    }


def _read_local_image(db: Session, asset_id: str) -> tuple[bytes, str]:
    location = db.scalar(
        select(models.AssetLocation)
        .where(
            models.AssetLocation.asset_id == asset_id,
            models.AssetLocation.provider == "local",
            models.AssetLocation.hydration_state == "hydrated",
        )
        .order_by(models.AssetLocation.created_at.desc())
    )
    if location is None:
        raise HTTPException(status_code=409, detail="selected image is not available locally")
    path = Path(location.uri.removeprefix("file://")).expanduser().resolve()
    settings = get_settings()
    roots = (settings.asset_root.resolve(), settings.cache_root.resolve())
    if not any(path == root or root in path.parents for root in roots):
        raise HTTPException(status_code=403, detail="image location is outside configured storage roots")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=409, detail="selected image could not be read") from exc
    if not payload:
        raise HTTPException(status_code=409, detail="selected image is empty")
    asset = get_or_404(db, models.Asset, asset_id)
    return payload, asset.mime_type or "image/jpeg"


def _caption_prompt(prompt: str, caption_format: str) -> str:
    if caption_format == "json":
        return f"{prompt.rstrip()}\n\nReturn only a valid JSON object describing this image. Do not use Markdown fences or commentary."
    return prompt


def _caption_json(caption: str) -> Any:
    try:
        value = json.loads(caption)
    except json.JSONDecodeError as exc:
        raise ValueError("caption is not valid JSON") from exc
    if not isinstance(value, (dict, list)):
        raise ValueError("caption JSON must be an object or array")
    return value


def _normalized_caption(caption: str, caption_format: str) -> str:
    if caption_format != "json":
        return caption
    try:
        value = _caption_json(caption)
    except ValueError as exc:
        raise LLMResponseError(str(exc)) from exc
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _operation_preview_token(
    draft_id: str,
    body: schemas.CaptionOperationRequest,
    draft_items: list[models.DatasetDraftItem],
    target_items: list[models.DatasetDraftItem],
) -> str:
    material = {
        "draft_id": draft_id,
        "operation": body.operation,
        "parameters": body.parameters,
        "scope": body.model_dump(
            include={"item_ids", "tag", "included", "caption_format", "all"},
            mode="json",
        ),
        "target_item_ids": [item.id for item in target_items],
        "draft": [
            {
                "id": item.id,
                "caption": item.caption,
                "caption_format": item.caption_format,
                "included": item.included,
                "tags": list(item.tags or []),
                "position": item.position,
            }
            for item in draft_items
        ],
    }
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _public_operation_result(result: dict[str, Any]) -> dict[str, Any]:
    changes = list(result["changes"])
    return {
        "matched": result["matched"],
        "changed": result["changed"],
        "changes": changes[:100],
        "truncated": len(changes) > 100,
    }


@router.get("/datasets")
def list_datasets(db: DB, project_id: str | None = None):
    query = select(models.Dataset).join(models.Project, models.Project.id == models.Dataset.project_id).where(
        models.Project.workspace_id == current_workspace(db).id,
    ).order_by(models.Dataset.updated_at.desc())
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
        caption = item.caption
        if body.caption_format == "json":
            try:
                caption = json.dumps(_caption_json(caption), ensure_ascii=False, separators=(",", ":"))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"{item.asset_id}: {exc}") from exc
        db.add(models.DatasetItem(
            dataset_version_id=version.id,
            **item.model_dump(exclude={"caption", "caption_format"}),
            caption=caption,
            caption_format=body.caption_format,
        ))
    db.flush()
    version.content_digest = content_digest(version, db.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)).all())
    record_activity(db, action="dataset.created", subject_type="dataset", subject_id=dataset.id, profile_id=actor.id, project_id=project.id, details={"version_id": version.id, "items": len(body.items)})
    db.commit()
    return {**_dump(dataset), "current_version_id": version.id}


@router.post("/dataset-versions/{version_id}/duplicate", status_code=status.HTTP_201_CREATED)
def duplicate_dataset_version(
    version_id: str,
    body: schemas.DatasetDuplicateRequest,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    source_version = get_or_404(db, models.DatasetVersion, version_id)
    source_dataset = get_or_404(db, models.Dataset, source_version.dataset_id)
    project = get_or_404(db, models.Project, source_dataset.project_id)
    actor = active_profile(db, x_profile_id)
    source_items = db.scalars(
        select(models.DatasetItem)
        .where(models.DatasetItem.dataset_version_id == source_version.id)
        .order_by(models.DatasetItem.position, models.DatasetItem.asset_id)
    ).all()
    dataset = models.Dataset(project_id=project.id, name=body.name, description=body.description)
    db.add(dataset)
    db.flush()
    version = models.DatasetVersion(
        dataset_id=dataset.id,
        version_number=1,
        name=source_version.name,
        source_uri=source_version.source_uri,
        caption_format=source_version.caption_format,
        trigger_words=list(source_version.trigger_words or []),
        published_by_profile_id=actor.id,
        parent_version_id=source_version.id,
        lineage_kind="dataset_duplicate",
    )
    db.add(version)
    db.flush()
    for item in source_items:
        db.add(
            models.DatasetItem(
                dataset_version_id=version.id,
                asset_id=item.asset_id,
                caption=item.caption,
                caption_format=item.caption_format,
                included=item.included,
                tags=list(item.tags or []),
                position=item.position,
            )
        )
    db.flush()
    version.content_digest = content_digest(
        version,
        db.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)).all(),
    )
    draft = None
    if body.open_draft:
        draft = models.DatasetDraft(dataset_id=dataset.id, base_version_id=version.id, created_by_profile_id=actor.id)
        db.add(draft)
        db.flush()
        for item in db.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)):
            db.add(
                models.DatasetDraftItem(
                    draft_id=draft.id,
                    source_item_id=item.id,
                    asset_id=item.asset_id,
                    caption=item.caption,
                    caption_format=item.caption_format,
                    included=item.included,
                    tags=list(item.tags or []),
                    position=item.position,
                )
            )
    record_activity(
        db,
        action="dataset.duplicated",
        subject_type="dataset",
        subject_id=dataset.id,
        profile_id=actor.id,
        project_id=project.id,
        details={"source_version_id": source_version.id, "version_id": version.id, "draft_id": draft.id if draft else None},
    )
    db.commit()
    return {
        "dataset_id": dataset.id,
        "current_version_id": version.id,
        "draft_id": draft.id if draft else None,
    }


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


@router.get("/datasets/{dataset_id}/drafts/active")
def active_draft(dataset_id: str, db: DB):
    get_or_404(db, models.Dataset, dataset_id)
    draft = db.scalar(
        select(models.DatasetDraft)
        .where(
            models.DatasetDraft.dataset_id == dataset_id,
            models.DatasetDraft.state == "active",
        )
        .order_by(models.DatasetDraft.created_at.desc())
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="no active working draft")
    return _dump_draft(db, draft)


@router.get("/dataset-versions/{version_id}/items")
def version_items(version_id: str, db: DB, included: bool | None = None, limit: int = Query(200, le=1000), offset: int = 0, paginated: bool = False):
    get_or_404(db, models.DatasetVersion, version_id)
    query = select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version_id).order_by(models.DatasetItem.position)
    if included is not None:
        query = query.where(models.DatasetItem.included == included)
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    rows = []
    for item in db.scalars(query.offset(offset).limit(limit)).all():
        rows.append(
            {
                **_dump(item),
                **_asset_fields(db, item.asset_id),
            }
        )
    return {"items": rows, "total": total, "limit": limit, "offset": offset} if paginated else rows


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
    return _dump_draft(db, get_or_404(db, models.DatasetDraft, draft_id))


@router.patch("/dataset-drafts/{draft_id}/caption-format")
def update_draft_caption_format(
    draft_id: str,
    body: schemas.CaptionFormatUpdate,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    items = list(db.scalars(select(models.DatasetDraftItem).where(models.DatasetDraftItem.draft_id == draft.id)).all())
    normalized_captions: dict[str, str] = {}
    if body.caption_format == "json":
        invalid = []
        for item in items:
            try:
                normalized_captions[item.id] = json.dumps(_caption_json(item.caption), ensure_ascii=False, separators=(",", ":"))
            except ValueError:
                invalid.append(item.id)
        if invalid:
            raise HTTPException(
                status_code=422,
                detail={"message": "Every caption must contain a JSON object or array before switching the draft to JSON.", "item_ids": invalid},
            )
    changes = []
    for item in items:
        normalized = normalized_captions.get(item.id, item.caption)
        if normalized != item.caption:
            changes.append({"item_id": item.id, "field": "caption", "before": item.caption, "after": normalized})
            item.caption = normalized
        if item.caption_format != body.caption_format:
            changes.append({"item_id": item.id, "field": "caption_format", "before": item.caption_format, "after": body.caption_format})
            item.caption_format = body.caption_format
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    actor = active_profile(db, x_profile_id)
    if changes:
        db.add(models.CaptionOperation(
            draft_id=draft.id,
            profile_id=actor.id,
            operation="set_format",
            parameters={"caption_format": body.caption_format},
            scope={"all": True},
            before_count=len(items),
            after_count=len({str(change["item_id"]) for change in changes}),
            preview={"matched": len(items), "changed": len(changes), "changes": changes},
        ))
    record_activity(
        db,
        action="caption.format_updated",
        subject_type="dataset_draft",
        subject_id=draft.id,
        profile_id=actor.id,
        project_id=dataset.project_id,
        details={"caption_format": body.caption_format, "count": len(items)},
    )
    db.commit()
    return {"caption_format": body.caption_format, "items": [{**_dump(item), **_asset_fields(db, item.asset_id)} for item in items]}


@router.patch("/dataset-drafts/{draft_id}/items/{item_id}")
def patch_draft_item(draft_id: str, item_id: str, body: schemas.DraftItemPatch, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    item = get_or_404(db, models.DatasetDraftItem, item_id)
    if item.draft_id != draft.id:
        raise HTTPException(status_code=404, detail="draft item not found")
    updates = body.model_dump(exclude_unset=True)
    next_caption = str(updates.get("caption", item.caption))
    next_format = str(updates.get("caption_format", item.caption_format))
    if next_format == "json":
        try:
            updates["caption"] = json.dumps(_caption_json(next_caption), ensure_ascii=False, separators=(",", ":"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    changes = [
        {"item_id": item.id, "field": key, "before": getattr(item, key), "after": value}
        for key, value in updates.items()
        if getattr(item, key) != value
    ]
    for key, value in updates.items():
        setattr(item, key, value)
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    actor = active_profile(db, x_profile_id)
    if changes:
        db.add(models.CaptionOperation(
            draft_id=draft.id,
            profile_id=actor.id,
            operation="edit_item",
            parameters={},
            scope={"item_ids": [item.id]},
            before_count=1,
            after_count=1,
            preview={"matched": 1, "changed": len(changes), "changes": changes},
        ))
    record_activity(db, action="caption.updated", subject_type="dataset_draft_item", subject_id=item.id, profile_id=actor.id, project_id=dataset.project_id)
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
                changes.append({"item_id": item.id, "field": "included", "before": item.included, "after": bool(operation.parameters["included"])})
                if mutate:
                    item.included = bool(operation.parameters["included"])
            continue
        if after != before:
            if item.caption_format == "json":
                try:
                    after = json.dumps(_caption_json(after), ensure_ascii=False, separators=(",", ":"))
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"operation would make JSON caption invalid for item {item.id}: {exc}",
                    ) from exc
            changes.append({"item_id": item.id, "field": "caption", "before": before, "after": after})
            if mutate:
                item.caption = after
    return {"matched": len(items), "changed": len(changes), "changes": changes}


@router.post("/dataset-drafts/{draft_id}/caption")
def generate_draft_captions(
    draft_id: str,
    body: schemas.CaptionGenerateRequest,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    query = select(models.DatasetDraftItem).where(models.DatasetDraftItem.draft_id == draft.id)
    if body.item_ids is not None:
        query = query.where(models.DatasetDraftItem.id.in_(body.item_ids))
    items = list(db.scalars(query.order_by(models.DatasetDraftItem.position)).all())
    if body.item_ids is not None and len(items) != len(body.item_ids):
        raise HTTPException(status_code=404, detail="one or more draft items were not found")
    if not items:
        raise HTTPException(status_code=422, detail="caption scope selected no draft items")
    preference = db.scalar(select(models.LocalPreference).where(models.LocalPreference.key == "operator_settings"))
    llm = (preference.value if preference is not None else {}).get("llm", {})
    provider = str(llm.get("provider") or "")
    model = str(body.model or llm.get("model") or "")
    prompt = _caption_prompt(body.prompt, body.caption_format)
    generator = PromptGenerator()
    before = {item.id: {"caption": item.caption, "caption_format": item.caption_format} for item in items}
    try:
        for item in items:
            image_bytes, mime_type = _read_local_image(db, item.asset_id)
            item.caption = _normalized_caption(
                generator.caption(
                    provider=provider,
                    model=model,
                    prompt=prompt,
                    image_bytes=image_bytes,
                    mime_type=mime_type,
                    base_url=llm.get("base_url"),
                ),
                body.caption_format,
            )
            item.caption_format = body.caption_format
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LLMResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    changes = [
        {
            "item_id": item.id,
            "field": "caption",
            "before": before[item.id]["caption"],
            "after": item.caption,
            "before_caption_format": before[item.id]["caption_format"],
            "after_caption_format": item.caption_format,
        }
        for item in items
        if before[item.id]["caption"] != item.caption or before[item.id]["caption_format"] != item.caption_format
    ]
    changed = len(changes)
    actor = active_profile(db, x_profile_id)
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    operation = models.CaptionOperation(
        draft_id=draft.id, profile_id=actor.id, operation="generate",
        parameters={"prompt": body.prompt, "provider": provider, "model": model, "caption_format": body.caption_format},
        scope={"item_ids": body.item_ids, "all": body.all},
        before_count=len(items), after_count=changed,
        preview={"matched": len(items), "changed": changed, "changes": changes},
    )
    db.add(operation)
    db.flush()
    record_activity(
        db, action="caption.generated", subject_type="dataset_draft", subject_id=draft.id,
        profile_id=actor.id, project_id=dataset.project_id,
        details={"operation_id": operation.id, "provider": provider, "model": model, "count": len(items), "changed": changed},
    )
    db.commit()
    rows = [{**_dump(item), **_asset_fields(db, item.asset_id)} for item in items]
    return {"items": rows, "changed": changed, "count": len(items), "operation_id": operation.id}


def _operation(draft_id: str, body: schemas.CaptionOperationRequest, db: Session, mutate: bool, profile_id: str | None):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    draft_items = list(db.scalars(
        select(models.DatasetDraftItem)
        .where(models.DatasetDraftItem.draft_id == draft.id)
        .order_by(models.DatasetDraftItem.position, models.DatasetDraftItem.id)
    ).all())
    requested_ids = set(body.item_ids or [])
    items = [
        item for item in draft_items
        if (not requested_ids or item.id in requested_ids)
        and (body.tag is None or body.tag in (item.tags or []))
        and (body.included is None or item.included == body.included)
        and (body.caption_format is None or item.caption_format == body.caption_format)
    ]
    if body.item_ids is not None and len(items) != len(body.item_ids):
        raise HTTPException(status_code=404, detail="one or more draft items were not found")
    preview_token = _operation_preview_token(draft.id, body, draft_items, items)
    if mutate:
        if not body.preview_token:
            raise HTTPException(status_code=409, detail="preview required before applying this operation")
        if body.preview_token != preview_token:
            raise HTTPException(status_code=409, detail="operation preview is stale; preview the current draft and scope again")
    result = _apply(body, items, mutate)
    public_result = {**_public_operation_result(result), "preview_token": preview_token, "target_item_ids": [item.id for item in items]}
    if mutate:
        actor = active_profile(db, profile_id)
        db.add(models.CaptionOperation(
            draft_id=draft.id,
            profile_id=actor.id,
            operation=body.operation,
            parameters=body.parameters,
            scope=body.model_dump(include={"item_ids", "tag", "included", "caption_format", "all"}, mode="json"),
            before_count=len(items),
            after_count=result["changed"],
            preview=result,
        ))
        dataset = get_or_404(db, models.Dataset, draft.dataset_id)
        record_activity(db, action="caption.batch_applied", subject_type="dataset_draft", subject_id=draft.id, profile_id=actor.id, project_id=dataset.project_id, details={"operation": body.operation, "changed": result["changed"]})
        db.commit()
    return public_result


@router.post("/dataset-drafts/{draft_id}/operations/preview")
def preview_operation(draft_id: str, body: schemas.CaptionOperationRequest, db: DB):
    return _operation(draft_id, body, db, False, None)


@router.post("/dataset-drafts/{draft_id}/operations")
def apply_operation(draft_id: str, body: schemas.CaptionOperationRequest, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    return _operation(draft_id, body, db, True, x_profile_id)


@router.get("/dataset-drafts/{draft_id}/operations")
def operation_history(draft_id: str, db: DB):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    operations = list(db.scalars(
        select(models.CaptionOperation)
        .where(models.CaptionOperation.draft_id == draft.id)
        .order_by(models.CaptionOperation.created_at.desc(), models.CaptionOperation.id.desc())
    ).all())
    undone_ids = {
        str(operation.parameters.get("operation_id"))
        for operation in operations
        if operation.operation == "undo" and operation.parameters.get("operation_id")
    }
    return [
        {
            **_dump(operation),
            "preview": {
                "matched": int((operation.preview or {}).get("matched", operation.before_count)),
                "changed": int((operation.preview or {}).get("changed", operation.after_count)),
            },
            "undone": operation.id in undone_ids,
            "can_undo": operation.operation != "undo" and operation.id not in undone_ids and bool((operation.preview or {}).get("changes")),
        }
        for operation in operations
    ]


@router.post("/dataset-drafts/{draft_id}/operations/{operation_id}/undo")
def undo_operation(
    draft_id: str,
    operation_id: str,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    operation = get_or_404(db, models.CaptionOperation, operation_id)
    if operation.draft_id != draft.id or operation.operation == "undo":
        raise HTTPException(status_code=404, detail="caption operation not found")
    prior_undoes = list(db.scalars(
        select(models.CaptionOperation).where(
            models.CaptionOperation.draft_id == draft.id,
            models.CaptionOperation.operation == "undo",
        )
    ).all())
    if any(str(row.parameters.get("operation_id")) == operation.id for row in prior_undoes):
        raise HTTPException(status_code=409, detail="caption operation was already undone")
    changes = list((operation.preview or {}).get("changes") or [])
    items_by_id = {
        item.id: item
        for item in db.scalars(select(models.DatasetDraftItem).where(models.DatasetDraftItem.draft_id == draft.id)).all()
    }
    for change in changes:
        item = items_by_id.get(str(change.get("item_id")))
        field = str(change.get("field") or ("included" if operation.operation == "set_included" else "caption"))
        if item is None or getattr(item, field) != change.get("after"):
            raise HTTPException(status_code=409, detail="draft changed after this operation; undo is no longer safe")
        if "after_caption_format" in change and item.caption_format != change.get("after_caption_format"):
            raise HTTPException(status_code=409, detail="draft caption format changed after this operation; undo is no longer safe")
    reversed_changes = []
    for change in changes:
        item = items_by_id[str(change["item_id"])]
        field = str(change.get("field") or ("included" if operation.operation == "set_included" else "caption"))
        current = getattr(item, field)
        setattr(item, field, change.get("before"))
        if field == "caption" and "before_caption_format" in change:
            item.caption_format = str(change["before_caption_format"])
        reversed_changes.append({"item_id": item.id, "field": field, "before": current, "after": change.get("before")})
    actor = active_profile(db, x_profile_id)
    undo = models.CaptionOperation(
        draft_id=draft.id,
        profile_id=actor.id,
        operation="undo",
        parameters={"operation_id": operation.id, "operation": operation.operation},
        scope={"item_ids": [str(change["item_id"]) for change in changes]},
        before_count=len(changes),
        after_count=len(changes),
        preview={"matched": len(changes), "changed": len(changes), "changes": reversed_changes},
    )
    db.add(undo)
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    record_activity(
        db,
        action="caption.batch_undone",
        subject_type="dataset_draft",
        subject_id=draft.id,
        profile_id=actor.id,
        project_id=dataset.project_id,
        details={"operation_id": operation.id, "operation": operation.operation, "changed": len(changes)},
    )
    db.commit()
    return {"operation_id": operation.id, "undone": len(changes)}


@router.post("/dataset-drafts/{draft_id}/publish", status_code=status.HTTP_201_CREATED)
def publish(draft_id: str, body: schemas.DraftPublish, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    base = get_or_404(db, models.DatasetVersion, draft.base_version_id)
    draft_items = list(db.scalars(select(models.DatasetDraftItem).where(models.DatasetDraftItem.draft_id == draft.id)))
    caption_formats = {item.caption_format for item in draft_items}
    if len(caption_formats) > 1:
        raise HTTPException(status_code=422, detail="draft captions must use one format; select Text or JSON for the whole draft")
    caption_format = next(iter(caption_formats), base.caption_format)
    next_number = (db.scalar(select(func.max(models.DatasetVersion.version_number)).where(models.DatasetVersion.dataset_id == dataset.id)) or 0) + 1
    actor = active_profile(db, x_profile_id)
    version = models.DatasetVersion(dataset_id=dataset.id, version_number=next_number, name=body.name, source_uri=base.source_uri, caption_format=caption_format, trigger_words=base.trigger_words, published_by_profile_id=actor.id, parent_version_id=base.id, lineage_kind="draft_publish")
    db.add(version)
    db.flush()
    for item in draft_items:
        db.add(models.DatasetItem(dataset_version_id=version.id, asset_id=item.asset_id, caption=item.caption, caption_format=item.caption_format, included=item.included, tags=list(item.tags or []), position=item.position))
    db.flush()
    version.content_digest = content_digest(
        version,
        db.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)).all(),
    )
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
