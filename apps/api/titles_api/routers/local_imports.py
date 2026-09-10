from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..image_provenance import extract_image_metadata
from ..services import active_profile, current_workspace, get_or_404, record_activity
from ..storage.repository import StorageRepository
from ..settings import get_settings

router = APIRouter(prefix="/local-imports", tags=["imports"])
DB = Annotated[Session, Depends(get_db)]
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
SIDECAR_SUFFIXES = {".txt", ".json"}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | SIDECAR_SUFFIXES


class LocalFile(BaseModel):
    path: str = Field(min_length=1, max_length=2048)
    content_base64: str
    mime_type: str | None = None
    last_modified: int | None = None


class LocalImportRequest(BaseModel):
    project_id: str
    dataset_name: str = Field(min_length=1, max_length=240)
    files: list[LocalFile] = Field(min_length=1, max_length=1000)


def _safe_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw.replace("\\", "/").lstrip("/"))
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path is not a safe relative folder path")
    return path


@router.post("", status_code=201)
def import_local_folder(body: LocalImportRequest, db: DB) -> dict[str, Any]:
    project = get_or_404(db, models.Project, body.project_id)
    workspace = current_workspace(db)
    if project.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="project not found in current workspace")
    repository = StorageRepository(db, workspace.id)
    decoded: dict[PurePosixPath, bytes] = {}
    failures: list[dict[str, str]] = []
    total_bytes = 0
    for item in body.files:
        try:
            path = _safe_path(item.path)
            if path in decoded:
                failures.append({"path": item.path, "error": "duplicate path in folder selection"})
                continue
            data = base64.b64decode(item.content_base64, validate=True)
            if len(data) > 25 * 1024 * 1024:
                raise ValueError("file exceeds 25 MiB limit")
            total_bytes += len(data)
            if total_bytes > 250 * 1024 * 1024:
                raise ValueError("folder exceeds 250 MiB limit")
            decoded[path] = data
        except (ValueError, binascii.Error) as exc:
            failures.append({"path": item.path, "error": str(exc)})

    unsupported = sorted(path for path in decoded if path.suffix.lower() not in SUPPORTED_SUFFIXES)
    failures.extend({"path": str(path), "error": f"unsupported file type: {path.suffix or 'no extension'}"} for path in unsupported)
    images = [(path, data) for path, data in decoded.items() if path.suffix.lower() in IMAGE_SUFFIXES]
    if not images:
        raise HTTPException(status_code=422, detail={"message": "folder contains no supported images", "failures": failures})
    dataset = models.Dataset(project_id=project.id, name=body.dataset_name, description="Imported from a local browser folder")
    db.add(dataset)
    db.flush()
    version = models.DatasetVersion(dataset_id=dataset.id, version_number=1, name="Local folder import", source_uri="local-folder://browser", status="published")
    db.add(version)
    db.flush()
    destination = get_settings().asset_root.resolve() / "local-imports" / dataset.id
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    written_targets: list[Path] = []
    imported = duplicates = 0
    dataset_items: dict[str, models.DatasetItem] = {}
    try:
        for position, (path, data) in enumerate(sorted(images)):
            digest = hashlib.sha256(data).hexdigest()
            image_metadata = extract_image_metadata(data)
            asset = db.scalar(select(models.Asset).where(models.Asset.project_id == project.id, models.Asset.sha256 == digest, models.Asset.kind == models.AssetKind.image))
            if asset is not None:
                duplicates += 1
                # A previous import may have persisted the bytes without image
                # metadata. Fill only missing facts so user-authored metadata
                # remains authoritative.
                if asset.provenance_kind == "legacy":
                    asset.provenance_kind = "local_import"
                merged = dict(asset.metadata_ or {})
                merged.setdefault("origin", "local_folder")
                for key, value in image_metadata.items():
                    merged.setdefault(key, value)
                asset.metadata_ = merged
                if image_metadata:
                    persisted = db.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
                    if persisted is None:
                        db.add(models.ImageMetadata(
                            asset_id=asset.id,
                            width=image_metadata.get("width"),
                            height=image_metadata.get("height"),
                            color_mode=image_metadata.get("color_mode"),
                            orientation=image_metadata.get("orientation"),
                            exif_summary=image_metadata.get("exif") or {},
                        ))
                    else:
                        for field in ("width", "height", "color_mode", "orientation"):
                            if getattr(persisted, field) is None and image_metadata.get(field) is not None:
                                setattr(persisted, field, image_metadata[field])
                        if not persisted.exif_summary and image_metadata.get("exif"):
                            persisted.exif_summary = image_metadata["exif"]
            else:
                metadata: dict[str, Any] = {
                    "category": "dataset_image",
                    "relative_path": str(path),
                    "origin": "local_folder",
                    **image_metadata,
                }
                sidecar_json = decoded.get(path.with_suffix(".json"))
                if sidecar_json is not None:
                    try:
                        parsed = json.loads(sidecar_json.decode("utf-8"))
                        if isinstance(parsed, dict):
                            metadata["sidecar"] = parsed
                        else:
                            failures.append({"path": str(path.with_suffix(".json")), "error": "metadata sidecar must contain a JSON object"})
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        failures.append({"path": str(path.with_suffix('.json')), "error": f"invalid JSON metadata: {exc}"})
                target = destination / f"{digest}{path.suffix.lower()}"
                target.write_bytes(data)
                written_targets.append(target)
                target.chmod(0o600)
                asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind=models.AssetKind.image, name=str(path), mime_type=mimetypes.guess_type(path.name)[0], sha256=digest, provenance_kind="local_import", metadata_=metadata)
                db.add(asset)
                db.flush()
                location = repository.attach_verified_local_location(asset_id=asset.id, uri=str(target), size=len(data), sha256=digest, mime_type=asset.mime_type or "application/octet-stream")
                location.relative_path = target.relative_to(get_settings().asset_root.resolve()).as_posix()
                if image_metadata:
                    db.add(models.ImageMetadata(
                        asset_id=asset.id,
                        width=image_metadata.get("width"),
                        height=image_metadata.get("height"),
                        color_mode=image_metadata.get("color_mode"),
                        orientation=image_metadata.get("orientation"),
                        exif_summary=image_metadata.get("exif") or {},
                    ))
                imported += 1
            caption_bytes = decoded.get(path.with_suffix(".txt"))
            try:
                caption = (
                    caption_bytes.decode("utf-8").strip()
                    if caption_bytes is not None
                    else str(image_metadata.get("caption") or "").strip()
                )
            except UnicodeDecodeError as exc:
                caption = ""
                failures.append({"path": str(path.with_suffix('.txt')), "error": f"caption is not UTF-8: {exc}"})
            existing_item = dataset_items.get(asset.id)
            if existing_item is None:
                existing_item = models.DatasetItem(dataset_version_id=version.id, asset_id=asset.id, caption=caption, position=position)
                dataset_items[asset.id] = existing_item
                db.add(existing_item)
            elif caption and not existing_item.caption:
                existing_item.caption = caption
        actor = active_profile(db, None)
        record_activity(db, action="dataset.local_imported", subject_type="dataset", subject_id=dataset.id, profile_id=actor.id, project_id=project.id, details={"imported": imported, "duplicates": duplicates, "failures": len(failures)})
        db.commit()
    except Exception:
        db.rollback()
        for target in written_targets:
            target.unlink(missing_ok=True)
        try:
            destination.rmdir()
        except OSError:
            pass
        raise
    return {"state": "completed_with_errors" if failures else "completed", "dataset_id": dataset.id, "version_id": version.id, "total_images": len(images), "imported": imported, "duplicates": duplicates, "failures": failures}
