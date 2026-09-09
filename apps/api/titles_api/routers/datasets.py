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
from ..integrations.config import S3Settings
from ..integrations.llm import LLMConfigurationError, LLMResponseError, PromptGenerator
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..settings import get_settings
from ..storage.s3_client import create_source_s3_client

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

def _version_item_subsets(db: Session, version_id: str) -> dict[str, list[dict[str, Any]]]:
    memberships = db.execute(
        select(models.DatasetSubsetItem, models.DatasetVersionSubset)
        .join(models.DatasetVersionSubset, models.DatasetVersionSubset.id == models.DatasetSubsetItem.subset_id)
        .where(models.DatasetVersionSubset.dataset_version_id == version_id)
        .order_by(models.DatasetVersionSubset.position, models.DatasetVersionSubset.key, models.DatasetSubsetItem.position)
    ).all()
    result: dict[str, list[dict[str, Any]]] = {}
    for membership, subset in memberships:
        result.setdefault(membership.dataset_item_id, []).append({
            "id": subset.id,
            "key": subset.key,
            "name": subset.name,
            "role": subset.role,
            "color_token": subset.color_token,
            "position": subset.position,
            "membership_role": membership.membership_role,
        })
    return result


def _draft_item_subsets(db: Session, draft_id: str) -> dict[str, list[dict[str, Any]]]:
    memberships = db.execute(
        select(models.DatasetDraftSubsetItem, models.DatasetDraftSubset)
        .join(models.DatasetDraftSubset, models.DatasetDraftSubset.id == models.DatasetDraftSubsetItem.subset_id)
        .where(models.DatasetDraftSubset.draft_id == draft_id)
        .order_by(models.DatasetDraftSubset.position, models.DatasetDraftSubset.key, models.DatasetDraftSubsetItem.position)
    ).all()
    result: dict[str, list[dict[str, Any]]] = {}
    for membership, subset in memberships:
        result.setdefault(membership.draft_item_id, []).append({
            "id": subset.id,
            "key": subset.key,
            "name": subset.name,
            "role": subset.role,
            "color_token": subset.color_token,
            "position": subset.position,
            "membership_role": membership.membership_role,
        })
    return result

def _version_subset_rows(db: Session, version_id: str) -> list[dict[str, Any]]:
    subsets = db.scalars(
        select(models.DatasetVersionSubset)
        .where(models.DatasetVersionSubset.dataset_version_id == version_id)
        .order_by(models.DatasetVersionSubset.position, models.DatasetVersionSubset.key)
    ).all()
    subset_ids = [subset.id for subset in subsets]
    memberships = db.scalars(
        select(models.DatasetSubsetItem)
        .where(models.DatasetSubsetItem.subset_id.in_(subset_ids))
        .order_by(models.DatasetSubsetItem.position)
    ).all() if subset_ids else []
    memberships_by_subset: dict[str, list[models.DatasetSubsetItem]] = {}
    for membership in memberships:
        memberships_by_subset.setdefault(membership.subset_id, []).append(membership)
    return [{
        **_dump(subset),
        "item_count": len(memberships_by_subset.get(subset.id, [])),
        "memberships": [_dump(membership) for membership in memberships_by_subset.get(subset.id, [])],
    } for subset in subsets]


def _draft_subset_rows(db: Session, draft_id: str) -> list[dict[str, Any]]:
    subsets = db.scalars(
        select(models.DatasetDraftSubset)
        .where(models.DatasetDraftSubset.draft_id == draft_id)
        .order_by(models.DatasetDraftSubset.position, models.DatasetDraftSubset.key)
    ).all()
    subset_ids = [subset.id for subset in subsets]
    memberships = db.scalars(
        select(models.DatasetDraftSubsetItem)
        .where(models.DatasetDraftSubsetItem.subset_id.in_(subset_ids))
        .order_by(models.DatasetDraftSubsetItem.position)
    ).all() if subset_ids else []
    memberships_by_subset: dict[str, list[models.DatasetDraftSubsetItem]] = {}
    for membership in memberships:
        memberships_by_subset.setdefault(membership.subset_id, []).append(membership)
    return [{
        **_dump(subset),
        "item_count": len(memberships_by_subset.get(subset.id, [])),
        "memberships": [_dump(membership) for membership in memberships_by_subset.get(subset.id, [])],
    } for subset in subsets]


def _asset_fields_bulk(db: Session, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not asset_ids:
        return {}
    assets = {
        asset.id: asset
        for asset in db.scalars(select(models.Asset).where(models.Asset.id.in_(asset_ids))).all()
    }
    images = {
        image.asset_id: image
        for image in db.scalars(select(models.ImageMetadata).where(models.ImageMetadata.asset_id.in_(asset_ids))).all()
    }
    locations_by_asset: dict[str, list[models.AssetLocation]] = {}
    for location in db.scalars(select(models.AssetLocation).where(models.AssetLocation.asset_id.in_(asset_ids))).all():
        locations_by_asset.setdefault(location.asset_id, []).append(location)
    fields: dict[str, dict[str, Any]] = {}
    for asset_id in asset_ids:
        asset = assets.get(asset_id)
        image = images.get(asset_id)
        locations = locations_by_asset.get(asset_id)
        fields[asset_id] = {
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
    return fields


def _dump_draft(db: Session, draft: models.DatasetDraft) -> dict[str, Any]:
    items = db.scalars(
        select(models.DatasetDraftItem)
        .where(models.DatasetDraftItem.draft_id == draft.id)
        .order_by(models.DatasetDraftItem.position)
    ).all()
    item_subsets = _draft_item_subsets(db, draft.id)
    fields_by_asset = _asset_fields_bulk(db, [item.asset_id for item in items])
    return {
        **_dump(draft),
        "items": [{**_dump(item), **fields_by_asset[item.asset_id], "subdatasets": item_subsets.get(item.id, [])} for item in items],
        "subsets": _draft_subset_rows(db, draft.id),
    }


def _recompute_version_digest(db: Session, version: models.DatasetVersion) -> str:
    items = db.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)).all()
    subsets = db.scalars(select(models.DatasetVersionSubset).where(models.DatasetVersionSubset.dataset_version_id == version.id)).all()
    subset_ids = [subset.id for subset in subsets]
    memberships = db.scalars(select(models.DatasetSubsetItem).where(models.DatasetSubsetItem.subset_id.in_(subset_ids))).all() if subset_ids else []
    version.content_digest = content_digest(version, items, subsets, memberships)
    return version.content_digest


def _create_default_subset(db: Session, version: models.DatasetVersion, items: list[models.DatasetItem]) -> models.DatasetVersionSubset:
    subset = models.DatasetVersionSubset(
        dataset_version_id=version.id,
        key="default",
        name="Default",
        role="custom",
        color_token="blue",
        position=0,
    )
    db.add(subset)
    db.flush()
    for position, item in enumerate(sorted(items, key=lambda value: (value.position, value.asset_id))):
        db.add(models.DatasetSubsetItem(
            subset_id=subset.id,
            dataset_item_id=item.id,
            membership_role="primary",
            position=position,
        ))
    db.flush()
    return subset


def _clone_version_composition(
    db: Session,
    source_version_id: str,
    target_version_id: str,
    item_ids: dict[str, str],
) -> None:
    source_subsets = db.scalars(
        select(models.DatasetVersionSubset)
        .where(models.DatasetVersionSubset.dataset_version_id == source_version_id)
        .order_by(models.DatasetVersionSubset.position)
    ).all()
    subset_ids: dict[str, str] = {}
    for source in source_subsets:
        target = models.DatasetVersionSubset(
            dataset_version_id=target_version_id,
            key=source.key,
            name=source.name,
            description=source.description,
            role=source.role,
            color_token=source.color_token,
            position=source.position,
        )
        db.add(target)
        db.flush()
        subset_ids[source.id] = target.id
    for source in source_subsets:
        if source.parent_subset_id:
            target = db.get(models.DatasetVersionSubset, subset_ids[source.id])
            target.parent_subset_id = subset_ids.get(source.parent_subset_id)
        memberships = db.scalars(select(models.DatasetSubsetItem).where(models.DatasetSubsetItem.subset_id == source.id)).all()
        for membership in memberships:
            target_item_id = item_ids.get(membership.dataset_item_id)
            if target_item_id:
                db.add(models.DatasetSubsetItem(
                    subset_id=subset_ids[source.id],
                    dataset_item_id=target_item_id,
                    membership_role=membership.membership_role,
                    position=membership.position,
                ))
    db.flush()


def _clone_version_to_draft(db: Session, version_id: str, draft_id: str, item_ids: dict[str, str]) -> None:
    source_subsets = db.scalars(
        select(models.DatasetVersionSubset)
        .where(models.DatasetVersionSubset.dataset_version_id == version_id)
        .order_by(models.DatasetVersionSubset.position)
    ).all()
    subset_ids: dict[str, str] = {}
    for source in source_subsets:
        target = models.DatasetDraftSubset(
            draft_id=draft_id,
            source_subset_id=source.id,
            key=source.key,
            name=source.name,
            description=source.description,
            role=source.role,
            color_token=source.color_token,
            position=source.position,
        )
        db.add(target)
        db.flush()
        subset_ids[source.id] = target.id
    for source in source_subsets:
        if source.parent_subset_id:
            target = db.get(models.DatasetDraftSubset, subset_ids[source.id])
            target.parent_subset_id = subset_ids.get(source.parent_subset_id)
        memberships = db.scalars(select(models.DatasetSubsetItem).where(models.DatasetSubsetItem.subset_id == source.id)).all()
        for membership in memberships:
            draft_item_id = item_ids.get(membership.dataset_item_id)
            if draft_item_id:
                db.add(models.DatasetDraftSubsetItem(
                    subset_id=subset_ids[source.id],
                    draft_item_id=draft_item_id,
                    membership_role=membership.membership_role,
                    position=membership.position,
                ))
    db.flush()

def _read_local_image(db: Session, asset_id: str) -> tuple[bytes, str]:
    asset = get_or_404(db, models.Asset, asset_id)
    local = db.scalar(
        select(models.AssetLocation)
        .where(
            models.AssetLocation.asset_id == asset_id,
            models.AssetLocation.provider == "local",
            models.AssetLocation.hydration_state == "hydrated",
        )
        .order_by(models.AssetLocation.created_at.desc())
    )
    local_error: HTTPException | None = None
    if local is not None:
        path = Path(local.uri.removeprefix("file://")).expanduser().resolve()
        settings = get_settings()
        roots = (settings.asset_root.resolve(), settings.cache_root.resolve())
        if not any(path == root or root in path.parents for root in roots):
            local_error = HTTPException(status_code=403, detail="image location is outside configured storage roots")
        else:
            try:
                payload = path.read_bytes()
            except OSError:
                local_error = HTTPException(status_code=409, detail="selected image could not be read")
            else:
                if payload:
                    return payload, asset.mime_type or "image/jpeg"
                local_error = HTTPException(status_code=409, detail="selected image is empty")

    expected_sha256 = str(asset.sha256 or "").lower()
    remote = None
    source = None
    for location in db.scalars(
        select(models.AssetLocation)
        .where(
            models.AssetLocation.asset_id == asset_id,
            models.AssetLocation.provider == "s3",
            models.AssetLocation.verification_state.in_(("verified", "available")),
        )
        .order_by(models.AssetLocation.created_at.desc())
    ):
        if not expected_sha256 or str(location.verified_sha256 or "").lower() != expected_sha256:
            continue
        candidate_source = db.get(models.ImportSource, location.source_id) if location.source_id else None
        if candidate_source is None or candidate_source.provider != "s3" or not candidate_source.is_active:
            continue
        if (
            not location.object_key
            or not (location.bucket or candidate_source.bucket)
            or (location.bucket and location.bucket != candidate_source.bucket)
        ):
            continue
        allowed = tuple(
            str(prefix).strip("/").rstrip("/")
            for prefix in (candidate_source.allowed_prefixes or ())
            if str(prefix).strip("/")
        )
        if allowed and not any(
            location.object_key == prefix or location.object_key.startswith(f"{prefix}/")
            for prefix in allowed
        ):
            continue
        remote, source = location, candidate_source
        break
    if remote is None or source is None:
        if local_error is not None:
            raise local_error
        raise HTTPException(
            status_code=409,
            detail=(
                "selected image has no usable local file or verified S3 location; "
                "hydrate it or configure an active verified S3 source"
            ),
        )

    settings = get_settings()
    maximum = int(getattr(settings, "wandb_image_max_bytes", 64 * 1024 * 1024))
    expected_size = remote.verified_size if remote.verified_size is not None else remote.size
    if expected_size is not None and int(expected_size) > maximum:
        raise HTTPException(status_code=409, detail="selected image exceeds the configured byte limit")
    try:
        s3_settings = S3Settings.for_source(
            endpoint_url=source.endpoint_url,
            bucket=source.bucket,
            allowed_prefixes=tuple(source.allowed_prefixes or ()),
            region=source.region,
            addressing_style=source.addressing_style,
            credential_env_prefix=source.credential_env_prefix,
        )
        client = create_source_s3_client(s3_settings)
    except Exception as exc:
        prefix = source.credential_env_prefix.upper()
        raise HTTPException(
            status_code=409,
            detail=(
                f"S3 credentials are unavailable for import source {source.name!r}; "
                f"configure {prefix}_ACCESS_KEY and {prefix}_SECRET_KEY"
            ),
        ) from exc

    request: dict[str, Any] = {
        "Bucket": remote.bucket or source.bucket,
        "Key": remote.object_key,
    }
    if remote.etag:
        request["IfMatch"] = remote.etag
    if remote.version_id:
        request["VersionId"] = remote.version_id
    body = None
    try:
        try:
            response = client.get_object(**request)
            body = response["Body"]
        except Exception as exc:
            raise HTTPException(status_code=409, detail="selected image could not be read from S3") from exc
        content_length = response.get("ContentLength")
        if content_length is not None and int(content_length) > maximum:
            raise HTTPException(status_code=409, detail="selected image exceeds the configured byte limit")
        if expected_size is not None and content_length is not None and int(content_length) != int(expected_size):
            raise HTTPException(status_code=409, detail="selected image size does not match verified metadata")
        payload = bytearray()
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = body.read(min(1024 * 1024, maximum - total + 1))
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise HTTPException(status_code=409, detail="selected image could not be read from S3")
            raw = bytes(chunk)
            total += len(raw)
            if total > maximum:
                raise HTTPException(status_code=409, detail="selected image exceeds the configured byte limit")
            payload.extend(raw)
            digest.update(raw)
        if expected_size is not None and total != int(expected_size):
            raise HTTPException(status_code=409, detail="selected image size changed during S3 read")
        if digest.hexdigest() != expected_sha256:
            raise HTTPException(status_code=409, detail="selected image digest differs from verified metadata")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail="selected image could not be read from S3") from exc
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    if not payload:
        raise HTTPException(status_code=409, detail="selected image is empty")
    return bytes(payload), asset.mime_type or "image/jpeg"


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
def list_datasets(
    db: DB,
    project_id: str | None = None,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
    paginated: bool = False,
):
    query = select(models.Dataset).join(models.Project, models.Project.id == models.Dataset.project_id).where(
        models.Project.workspace_id == current_workspace(db).id,
    )
    if project_id:
        query = query.where(models.Dataset.project_id == project_id)
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    dataset_rows = db.scalars(
        query.order_by(models.Dataset.updated_at.desc()).offset(offset).limit(limit)
    ).all()
    dataset_ids = [row.id for row in dataset_rows]
    project_ids = {row.project_id for row in dataset_rows}
    projects = {
        project.id: project
        for project in db.scalars(select(models.Project).where(models.Project.id.in_(project_ids))).all()
    } if project_ids else {}
    latest_by_dataset: dict[str, models.DatasetVersion] = {}
    item_counts: dict[str, int] = {}
    if dataset_ids:
        latest_numbers = (
            select(
                models.DatasetVersion.dataset_id.label("dataset_id"),
                func.max(models.DatasetVersion.version_number).label("version_number"),
            )
            .where(models.DatasetVersion.dataset_id.in_(dataset_ids))
            .group_by(models.DatasetVersion.dataset_id)
            .subquery()
        )
        latest_by_dataset = {
            version.dataset_id: version
            for version in db.scalars(
                select(models.DatasetVersion).join(
                    latest_numbers,
                    (models.DatasetVersion.dataset_id == latest_numbers.c.dataset_id)
                    & (models.DatasetVersion.version_number == latest_numbers.c.version_number),
                )
            ).all()
        }
        item_counts = dict(
            db.execute(
                select(models.DatasetItem.dataset_version_id, func.count())
                .where(
                    models.DatasetItem.dataset_version_id.in_(
                        [version.id for version in latest_by_dataset.values()]
                    )
                )
                .group_by(models.DatasetItem.dataset_version_id)
            ).all()
        )
    rows = []
    for row in dataset_rows:
        project = projects.get(row.project_id)
        version = latest_by_dataset.get(row.id)
        rows.append({
            **_dump(row),
            "project_name": project.title if project else None,
            "current_version": version.version_number if version else None,
            "current_version_id": version.id if version else None,
            "item_count": item_counts.get(version.id, 0) if version else 0,
        })
    return {"items": rows, "total": total, "limit": limit, "offset": offset} if paginated else rows


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
    created_items = db.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)).all()
    _create_default_subset(db, version, created_items)
    _recompute_version_digest(db, version)
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
    target_items = db.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)).all()
    target_by_asset = {item.asset_id: item.id for item in target_items}
    _clone_version_composition(
        db,
        source_version.id,
        version.id,
        {item.id: target_by_asset[item.asset_id] for item in source_items},
    )
    _recompute_version_digest(db, version)
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
        db.flush()
        draft_items = db.scalars(select(models.DatasetDraftItem).where(models.DatasetDraftItem.draft_id == draft.id)).all()
        draft_by_asset = {item.asset_id: item.id for item in draft_items}
        _clone_version_to_draft(
            db,
            version.id,
            draft.id,
            {item.id: draft_by_asset[item.asset_id] for item in target_items},
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
    version_rows = []
    for version in versions:
        subsets = _version_subset_rows(db, version.id)
        version_rows.append({
            **_dump(version),
            "item_count": int(db.scalar(select(func.count()).select_from(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)) or 0),
            "subset_count": len(subsets),
            "subsets": subsets,
        })
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
        "subsets": _version_subset_rows(db, current.id) if current else [],
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
def version_items(
    version_id: str,
    db: DB,
    included: bool | None = None,
    subset_id: str | None = None,
    sort: str = "position",
    limit: int = Query(200, le=1000),
    offset: int = 0,
    paginated: bool = False,
):
    get_or_404(db, models.DatasetVersion, version_id)
    if sort not in {"position", "subdataset"}:
        raise HTTPException(status_code=422, detail="sort must be position or subdataset")
    query = select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version_id)
    if included is not None:
        query = query.where(models.DatasetItem.included == included)
    if subset_id:
        subset = get_or_404(db, models.DatasetVersionSubset, subset_id)
        if subset.dataset_version_id != version_id:
            raise HTTPException(status_code=400, detail="sub-dataset belongs to a different dataset version")
        query = query.join(models.DatasetSubsetItem, models.DatasetSubsetItem.dataset_item_id == models.DatasetItem.id).where(
            models.DatasetSubsetItem.subset_id == subset_id
        )
    if sort == "subdataset" and subset_id:
        query = query.order_by(models.DatasetSubsetItem.position, models.DatasetItem.position)
    elif sort == "subdataset":
        query = query.outerjoin(
            models.DatasetSubsetItem,
            (models.DatasetSubsetItem.dataset_item_id == models.DatasetItem.id)
            & (models.DatasetSubsetItem.membership_role == "primary"),
        ).outerjoin(models.DatasetVersionSubset, models.DatasetVersionSubset.id == models.DatasetSubsetItem.subset_id).order_by(
            func.coalesce(models.DatasetVersionSubset.position, 2147483647),
            func.coalesce(models.DatasetVersionSubset.key, ""),
            models.DatasetItem.position,
        )
    else:
        query = query.order_by(models.DatasetItem.position)
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    item_subsets = _version_item_subsets(db, version_id)
    rows = []
    for item in db.scalars(query.offset(offset).limit(limit)).all():
        rows.append(
            {
                **_dump(item),
                **_asset_fields(db, item.asset_id),
                "subdatasets": item_subsets.get(item.id, []),
            }
        )
    return {"items": rows, "total": total, "limit": limit, "offset": offset} if paginated else rows


@router.get("/dataset-versions/{version_id}")
def dataset_version(version_id: str, db: DB):
    version = get_or_404(db, models.DatasetVersion, version_id)
    dataset = get_or_404(db, models.Dataset, version.dataset_id)
    subsets = _version_subset_rows(db, version.id)
    return {
        **_dump(version),
        "dataset_name": dataset.name,
        "project_id": dataset.project_id,
        "item_count": int(db.scalar(select(func.count()).select_from(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id)) or 0),
        "subset_count": len(subsets),
        "subsets": subsets,
    }


@router.get("/dataset-versions/{version_id}/subsets")
def version_subsets(version_id: str, db: DB):
    get_or_404(db, models.DatasetVersion, version_id)
    return _version_subset_rows(db, version_id)



@router.post("/dataset-drafts/{draft_id}/subsets", status_code=status.HTTP_201_CREATED)
def create_draft_subset(
    draft_id: str,
    body: schemas.DatasetSubsetCreate,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if draft.state != "active":
        raise HTTPException(status_code=409, detail="draft is not active")
    if body.parent_subset_id:
        parent = get_or_404(db, models.DatasetDraftSubset, body.parent_subset_id)
        if parent.draft_id != draft.id:
            raise HTTPException(status_code=400, detail="parent subset belongs to a different draft")
    subset = models.DatasetDraftSubset(draft_id=draft.id, **body.model_dump())
    db.add(subset)
    db.flush()
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    record_activity(db, action="dataset_subset.created", subject_type="dataset_draft", subject_id=draft.id, profile_id=active_profile(db, x_profile_id).id, project_id=dataset.project_id, details={"subset_id": subset.id, "key": subset.key})
    db.commit()
    return {**_dump(subset), "item_count": 0, "memberships": []}


@router.patch("/dataset-drafts/{draft_id}/subsets/{subset_id}")
def update_draft_subset(
    draft_id: str,
    subset_id: str,
    body: schemas.DatasetSubsetUpdate,
    db: DB,
):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    subset = get_or_404(db, models.DatasetDraftSubset, subset_id)
    if draft.state != "active" or subset.draft_id != draft.id:
        raise HTTPException(status_code=409, detail="subset is not editable in this draft")
    changes = body.model_dump(exclude_unset=True)
    if "parent_subset_id" in changes and changes["parent_subset_id"]:
        parent = get_or_404(db, models.DatasetDraftSubset, str(changes["parent_subset_id"]))
        if parent.draft_id != draft.id or parent.id == subset.id:
            raise HTTPException(status_code=400, detail="invalid parent subset")
    for key, value in changes.items():
        setattr(subset, key, value)
    db.commit()
    return {**_dump(subset), "memberships": [row for row in _draft_subset_rows(db, draft.id) if row["id"] == subset.id][0]["memberships"]}


@router.delete("/dataset-drafts/{draft_id}/subsets/{subset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_draft_subset(draft_id: str, subset_id: str, db: DB):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    subset = get_or_404(db, models.DatasetDraftSubset, subset_id)
    if draft.state != "active" or subset.draft_id != draft.id:
        raise HTTPException(status_code=409, detail="subset is not editable in this draft")
    children = db.scalar(select(func.count()).select_from(models.DatasetDraftSubset).where(models.DatasetDraftSubset.parent_subset_id == subset.id))
    if children:
        raise HTTPException(status_code=409, detail="move or remove child subsets first")
    db.delete(subset)
    db.commit()


@router.post("/dataset-drafts/{draft_id}/subset-assignments")
def assign_draft_subset(
    draft_id: str,
    body: schemas.DatasetSubsetAssignment,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    subset = get_or_404(db, models.DatasetDraftSubset, body.subset_id)
    if draft.state != "active" or subset.draft_id != draft.id:
        raise HTTPException(status_code=409, detail="subset is not editable in this draft")
    items = db.scalars(select(models.DatasetDraftItem).where(models.DatasetDraftItem.id.in_(body.draft_item_ids))).all()
    if len(items) != len(set(body.draft_item_ids)) or any(item.draft_id != draft.id for item in items):
        raise HTTPException(status_code=400, detail="one or more items do not belong to this draft")
    if body.membership_role == "primary":
        existing = db.scalars(
            select(models.DatasetDraftSubsetItem)
            .join(models.DatasetDraftSubset, models.DatasetDraftSubset.id == models.DatasetDraftSubsetItem.subset_id)
            .where(
                models.DatasetDraftSubset.draft_id == draft.id,
                models.DatasetDraftSubsetItem.draft_item_id.in_(body.draft_item_ids),
                models.DatasetDraftSubsetItem.membership_role == "primary",
            )
        ).all()
        for membership in existing:
            db.delete(membership)
        db.flush()
    for position, item_id in enumerate(body.draft_item_ids):
        existing = db.scalar(select(models.DatasetDraftSubsetItem).where(
            models.DatasetDraftSubsetItem.subset_id == subset.id,
            models.DatasetDraftSubsetItem.draft_item_id == item_id,
        ))
        if existing:
            existing.membership_role = body.membership_role
            existing.position = position
        else:
            db.add(models.DatasetDraftSubsetItem(
                subset_id=subset.id,
                draft_item_id=item_id,
                membership_role=body.membership_role,
                position=position,
            ))
    dataset = get_or_404(db, models.Dataset, draft.dataset_id)
    record_activity(db, action="dataset_subset.assigned", subject_type="dataset_draft", subject_id=draft.id, profile_id=active_profile(db, x_profile_id).id, project_id=dataset.project_id, details={"subset_id": subset.id, "item_count": len(body.draft_item_ids), "membership_role": body.membership_role})
    db.commit()
    return {"assigned": len(body.draft_item_ids), "subsets": _draft_subset_rows(db, draft.id)}

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
    item_ids: dict[str, str] = {}
    for item in db.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == base.id)):
        draft_item = models.DatasetDraftItem(draft_id=draft.id, source_item_id=item.id, asset_id=item.asset_id, caption=item.caption, caption_format=item.caption_format, included=item.included, tags=item.tags, position=item.position)
        db.add(draft_item)
        db.flush()
        item_ids[item.id] = draft_item.id
    _clone_version_to_draft(db, base.id, draft.id, item_ids)
    record_activity(db, action="dataset_draft.created", subject_type="dataset_draft", subject_id=draft.id, profile_id=actor.id, project_id=dataset.project_id)
    db.commit()
    return _dump(draft)


@router.get("/dataset-drafts/{draft_id}")
def draft(draft_id: str, db: DB):
    return _dump_draft(db, get_or_404(db, models.DatasetDraft, draft_id))

@router.get("/dataset-drafts/{draft_id}/items")
def draft_items(
    draft_id: str,
    db: DB,
    included: bool | None = None,
    subset_id: str | None = None,
    sort: str = "position",
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
    paginated: bool = False,
):
    draft = get_or_404(db, models.DatasetDraft, draft_id)
    if sort not in {"position", "subdataset"}:
        raise HTTPException(status_code=422, detail="sort must be position or subdataset")
    query = select(models.DatasetDraftItem).where(models.DatasetDraftItem.draft_id == draft.id)
    if included is not None:
        query = query.where(models.DatasetDraftItem.included == included)
    if subset_id:
        subset = get_or_404(db, models.DatasetDraftSubset, subset_id)
        if subset.draft_id != draft.id:
            raise HTTPException(status_code=400, detail="sub-dataset belongs to a different dataset draft")
        query = query.join(
            models.DatasetDraftSubsetItem,
            models.DatasetDraftSubsetItem.draft_item_id == models.DatasetDraftItem.id,
        ).where(models.DatasetDraftSubsetItem.subset_id == subset_id)
    if sort == "subdataset" and subset_id:
        query = query.order_by(models.DatasetDraftSubsetItem.position, models.DatasetDraftItem.position)
    elif sort == "subdataset":
        query = query.outerjoin(
            models.DatasetDraftSubsetItem,
            (models.DatasetDraftSubsetItem.draft_item_id == models.DatasetDraftItem.id)
            & (models.DatasetDraftSubsetItem.membership_role == "primary"),
        ).outerjoin(
            models.DatasetDraftSubset,
            models.DatasetDraftSubset.id == models.DatasetDraftSubsetItem.subset_id,
        ).order_by(
            func.coalesce(models.DatasetDraftSubset.position, 2147483647),
            func.coalesce(models.DatasetDraftSubset.key, ""),
            models.DatasetDraftItem.position,
        ).distinct()
    else:
        query = query.order_by(models.DatasetDraftItem.position)
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    items = db.scalars(query.offset(offset).limit(limit)).all()
    item_subsets = _draft_item_subsets(db, draft.id)
    fields_by_asset = _asset_fields_bulk(db, [item.asset_id for item in items])
    rows = [
        {
            **_dump(item),
            **fields_by_asset[item.asset_id],
            "subdatasets": item_subsets.get(item.id, []),
        }
        for item in items
    ]
    return {"items": rows, "total": total, "limit": limit, "offset": offset} if paginated else rows


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
    draft_subsets = db.scalars(select(models.DatasetDraftSubset).where(models.DatasetDraftSubset.draft_id == draft.id)).all()
    if not draft_subsets:
        raise HTTPException(status_code=422, detail="dataset version must contain at least one sub-dataset")
    primary_memberships = db.scalars(
        select(models.DatasetDraftSubsetItem)
        .join(models.DatasetDraftSubset, models.DatasetDraftSubset.id == models.DatasetDraftSubsetItem.subset_id)
        .where(
            models.DatasetDraftSubset.draft_id == draft.id,
            models.DatasetDraftSubsetItem.membership_role == "primary",
        )
    ).all()
    primary_counts: dict[str, int] = {}
    for membership in primary_memberships:
        primary_counts[membership.draft_item_id] = primary_counts.get(membership.draft_item_id, 0) + 1
    invalid_items = [item.id for item in draft_items if item.included and primary_counts.get(item.id, 0) != 1]
    if invalid_items:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "every included item must have exactly one primary sub-dataset membership",
                "item_ids": invalid_items[:100],
                "truncated": len(invalid_items) > 100,
            },
        )
    caption_formats = {item.caption_format for item in draft_items}
    if len(caption_formats) > 1:
        raise HTTPException(status_code=422, detail="draft captions must use one format; select Text or JSON for the whole draft")
    caption_format = next(iter(caption_formats), base.caption_format)
    next_number = (db.scalar(select(func.max(models.DatasetVersion.version_number)).where(models.DatasetVersion.dataset_id == dataset.id)) or 0) + 1
    actor = active_profile(db, x_profile_id)
    version = models.DatasetVersion(dataset_id=dataset.id, version_number=next_number, name=body.name, source_uri=base.source_uri, caption_format=caption_format, trigger_words=base.trigger_words, published_by_profile_id=actor.id, parent_version_id=base.id, lineage_kind="draft_publish")
    db.add(version)
    db.flush()
    published_item_ids: dict[str, str] = {}
    for item in draft_items:
        published_item = models.DatasetItem(dataset_version_id=version.id, asset_id=item.asset_id, caption=item.caption, caption_format=item.caption_format, included=item.included, tags=list(item.tags or []), position=item.position)
        db.add(published_item)
        db.flush()
        published_item_ids[item.id] = published_item.id
    draft_subsets = db.scalars(select(models.DatasetDraftSubset).where(models.DatasetDraftSubset.draft_id == draft.id).order_by(models.DatasetDraftSubset.position)).all()
    published_subset_ids: dict[str, str] = {}
    for subset in draft_subsets:
        published_subset = models.DatasetVersionSubset(
            dataset_version_id=version.id,
            key=subset.key,
            name=subset.name,
            description=subset.description,
            role=subset.role,
            color_token=subset.color_token,
            position=subset.position,
        )
        db.add(published_subset)
        db.flush()
        published_subset_ids[subset.id] = published_subset.id
    for subset in draft_subsets:
        if subset.parent_subset_id:
            db.get(models.DatasetVersionSubset, published_subset_ids[subset.id]).parent_subset_id = published_subset_ids.get(subset.parent_subset_id)
        memberships = db.scalars(select(models.DatasetDraftSubsetItem).where(models.DatasetDraftSubsetItem.subset_id == subset.id)).all()
        for membership in memberships:
            db.add(models.DatasetSubsetItem(
                subset_id=published_subset_ids[subset.id],
                dataset_item_id=published_item_ids[membership.draft_item_id],
                membership_role=membership.membership_role,
                position=membership.position,
            ))
    db.flush()
    _recompute_version_digest(db, version)
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
