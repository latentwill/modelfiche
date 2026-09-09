from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.asset_cache import AssetCache, CacheEntry
from titles_api.image_provenance import extract_image_metadata
from titles_api.integrations.config import S3Settings
from titles_api.integrations.s3.browser import PrefixAccessError, S3Browser
from titles_api.settings import get_settings
from titles_api.storage.fal_artifacts import MAX_ARTIFACT_BYTES
from titles_api.storage.network import SafeHttpTransport

from .runner import JobContext


class ImageLineageRepairHandler:
    """Move legacy cache-backed image records to durable, source-verified storage."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cache: AssetCache,
        *,
        artifact_transport: SafeHttpTransport | None = None,
        s3_browser_factory: Callable[[S3Settings], S3Browser] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.cache = cache
        self.artifact_transport = artifact_transport or SafeHttpTransport()
        self.s3_browser_factory = s3_browser_factory or S3Browser.from_settings

    def __call__(self, context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            raise ValueError("image lineage repair requires project_id")
        settings = get_settings()
        asset_root = settings.asset_root.resolve()
        cache_root = settings.cache_root.resolve()
        with self.session_factory() as session:
            project = session.get(models.Project, project_id)
            if project is None:
                raise LookupError("project not found")
            asset_ids = list(
                session.scalars(
                    select(models.Asset.id)
                    .join(
                        models.AssetLocation,
                        models.AssetLocation.asset_id == models.Asset.id,
                    )
                    .where(
                        models.Asset.project_id == project.id,
                        models.Asset.workspace_id == project.workspace_id,
                        models.Asset.kind == models.AssetKind.image,
                        models.AssetLocation.provider == "local",
                        models.AssetLocation.hydration_state == "hydrated",
                    )
                    .distinct()
                    .order_by(models.Asset.id)
                )
            )

        repaired = skipped = failed = 0
        failures: list[dict[str, str]] = []
        total = max(len(asset_ids), 1)
        for index, asset_id in enumerate(asset_ids):
            if context.cancellation_requested():
                return self._result(repaired, skipped, failed, failures, canceled=True)
            try:
                outcome = self._repair_asset(
                    asset_id, asset_root=asset_root, cache_root=cache_root
                )
            except Exception as exc:
                failed += 1
                if len(failures) < 100:
                    failures.append({"asset_id": asset_id, "error": type(exc).__name__})
            else:
                if outcome:
                    repaired += 1
                else:
                    skipped += 1
            context.progress(
                (index + 1) / total,
                result_patch={
                    "repaired": repaired,
                    "skipped": skipped,
                    "failed": failed,
                },
            )
        return self._result(repaired, skipped, failed, failures)

    def _repair_asset(
        self, asset_id: str, *, asset_root: Path, cache_root: Path
    ) -> bool:
        with self.session_factory() as session:
            asset = session.get(models.Asset, asset_id)
            if asset is None:
                return False
            local_locations = list(
                session.scalars(
                    select(models.AssetLocation)
                    .where(
                        models.AssetLocation.asset_id == asset.id,
                        models.AssetLocation.provider == "local",
                    )
                    .order_by(models.AssetLocation.created_at)
                )
            )
            legacy_locations = [
                location
                for location in local_locations
                if _is_within(_location_path(location), cache_root)
            ]
            if not legacy_locations:
                return False
            expected_sha256 = asset.sha256 or next(
                (
                    location.verified_sha256
                    for location in legacy_locations
                    if location.verified_sha256
                ),
                None,
            )
            expected_size = next(
                (
                    location.verified_size or location.size
                    for location in legacy_locations
                    if location.verified_size or location.size
                ),
                None,
            )
            source_url = session.scalar(
                select(models.EvalOutput.provider_metadata)
                .where(models.EvalOutput.asset_id == asset.id)
                .limit(1)
            )
            source_url = (
                source_url.get("source_url") if isinstance(source_url, dict) else None
            )
            durable_locations = (
                list(
                    session.scalars(
                        select(models.AssetLocation).where(
                            models.AssetLocation.provider == "local",
                            models.AssetLocation.verified_sha256 == expected_sha256,
                            models.AssetLocation.asset_id != asset.id,
                        )
                    )
                )
                if expected_sha256
                else []
            )
            remote_rows = session.execute(
                select(models.AssetLocation, models.ImportSource)
                .join(
                    models.ImportSource,
                    models.ImportSource.id == models.AssetLocation.source_id,
                )
                .where(
                    models.AssetLocation.asset_id == asset.id,
                    models.AssetLocation.provider == "s3",
                    models.AssetLocation.object_key.is_not(None),
                    models.AssetLocation.hydration_state != "remote_missing",
                )
                .order_by(
                    models.ImportSource.is_active.desc(),
                    models.AssetLocation.last_verified_at.desc(),
                )
            ).all()
            existing_path = next(
                (
                    path
                    for path in map(
                        _location_path, [*durable_locations, *legacy_locations]
                    )
                    if path.is_file()
                    and (_is_within(path, asset_root) or _is_within(path, cache_root))
                ),
                None,
            )

        entry: CacheEntry | None = None
        errors: list[Exception] = []
        if existing_path is not None:
            try:
                entry = _verified_existing_entry(
                    existing_path,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )
            except Exception as exc:
                errors.append(exc)
        if entry is None and isinstance(source_url, str) and source_url:
            try:
                entry = self._download_https(
                    source_url,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )
            except Exception as exc:
                errors.append(exc)
        if entry is None:
            for remote, source in remote_rows:
                try:
                    entry = self._download_s3(
                        remote, source, expected_sha256=expected_sha256
                    )
                    break
                except Exception as exc:
                    errors.append(exc)
        if entry is None:
            if errors:
                raise errors[-1]
            self._mark_unrecoverable(asset_id, cache_root=cache_root)
            raise LookupError("image has no recoverable source")

        durable = self.cache.persist(entry, asset_root)
        image_metadata = extract_image_metadata(durable.path.read_bytes())
        relative_path = str(durable.path.relative_to(asset_root))
        with self.session_factory.begin() as session:
            asset = session.get(models.Asset, asset_id)
            if asset is None:
                raise LookupError("asset was removed during image repair")
            locations = list(
                session.scalars(
                    select(models.AssetLocation).where(
                        models.AssetLocation.asset_id == asset.id,
                        models.AssetLocation.provider == "local",
                    )
                )
            )
            repaired_locations = [
                location
                for location in locations
                if _is_within(_location_path(location), cache_root)
            ]
            if not repaired_locations:
                return False
            for location in repaired_locations:
                location.uri = str(durable.path)
                location.relative_path = relative_path
                location.size = durable.size
                location.verified_size = durable.size
                location.verified_sha256 = durable.sha256
                location.verification_state = "available"
                location.hydration_state = "hydrated"
                location.last_verified_at = models.utcnow()
                location.repair_attribution = "image_lineage.durable_source_repair"
            asset.sha256 = durable.sha256
            blob = session.scalar(
                select(models.ContentBlob).where(
                    models.ContentBlob.sha256 == durable.sha256
                )
            )
            if blob is None:
                blob = models.ContentBlob(
                    sha256=durable.sha256,
                    size=durable.size,
                    mime_type=asset.mime_type or "application/octet-stream",
                )
                session.add(blob)
                session.flush()
            asset.content_blob_id = blob.id
            if asset.preferred_location_id is None or any(
                location.id == asset.preferred_location_id
                for location in repaired_locations
            ):
                asset.preferred_location_id = repaired_locations[0].id
            if asset.origin_location_id is None:
                asset.origin_location_id = repaired_locations[0].id
            if image_metadata:
                merged = dict(asset.metadata_ or {})
                for key, value in image_metadata.items():
                    merged.setdefault(key, value)
                asset.metadata_ = merged
                persisted = session.scalar(
                    select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id)
                )
                if persisted is None:
                    session.add(models.ImageMetadata(
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
        return True

    def _mark_unrecoverable(self, asset_id: str, *, cache_root: Path) -> None:
        with self.session_factory.begin() as session:
            asset = session.get(models.Asset, asset_id)
            if asset is None:
                return
            locations = list(
                session.scalars(
                    select(models.AssetLocation).where(
                        models.AssetLocation.asset_id == asset.id,
                        models.AssetLocation.provider == "local",
                    )
                )
            )
            missing = [
                location
                for location in locations
                if _is_within(_location_path(location), cache_root)
            ]
            for location in missing:
                location.verification_state = "unavailable"
                location.hydration_state = "missing"
                location.repair_attribution = "image_lineage.source_unavailable"
            if any(location.id == asset.preferred_location_id for location in missing):
                asset.preferred_location_id = None

    def _download_https(
        self, url: str, *, expected_sha256: str | None, expected_size: int | None
    ) -> CacheEntry:
        response = self.artifact_transport.request(
            "GET",
            url,
            max_bytes=MAX_ARTIFACT_BYTES,
            max_error_bytes=64 * 1024,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            raise ValueError(f"image source returned HTTP {response.status_code}")
        if expected_size is not None and len(response.body) != expected_size:
            raise ValueError("image source size drifted")
        return self.cache.hydrate(
            len(response.body),
            lambda stream: stream.write(response.body),
            expected_sha256=expected_sha256,
        )

    def _download_s3(
        self,
        remote: models.AssetLocation,
        source: models.ImportSource,
        *,
        expected_sha256: str | None,
    ) -> CacheEntry:
        settings = S3Settings.for_source(
            endpoint_url=source.endpoint_url,
            bucket=source.bucket,
            allowed_prefixes=tuple(source.allowed_prefixes or ()),
            region=source.region,
            addressing_style=source.addressing_style,
            credential_env_prefix=source.credential_env_prefix,
        )
        browser = self.s3_browser_factory(settings)
        object_key = remote.object_key or ""
        expected_size = remote.verified_size or remote.size
        if expected_size is None:
            expected_size = int(
                browser.client.head_object(Bucket=settings.bucket, Key=object_key)[
                    "ContentLength"
                ]
            )

        def download(stream) -> None:
            try:
                browser.download(object_key, stream, etag=remote.etag)
            except PrefixAccessError:
                _download_recorded_s3_object(
                    browser.client,
                    bucket=remote.bucket or settings.bucket,
                    object_key=object_key,
                    target=stream,
                    etag=remote.etag,
                )

        return self.cache.hydrate(
            expected_size,
            download,
            expected_sha256=expected_sha256 or remote.verified_sha256,
        )

    @staticmethod
    def _result(
        repaired: int,
        skipped: int,
        failed: int,
        failures: list[dict[str, str]],
        *,
        canceled: bool = False,
    ) -> dict[str, Any]:
        return {
            "repaired": repaired,
            "skipped": skipped,
            "failed": failed,
            "failures": failures,
            "canceled": canceled,
        }


def _location_path(location: models.AssetLocation) -> Path:
    return Path(location.uri.removeprefix("file://")).expanduser().resolve()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _download_recorded_s3_object(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    target: Any,
    etag: str | None,
) -> None:
    """Read the exact immutable key already recorded on the asset location."""
    request: dict[str, Any] = {"Bucket": bucket, "Key": object_key}
    if etag:
        request["IfMatch"] = etag
    response = client.get_object(**request)
    body = response["Body"]
    response_etag = response.get("ETag")
    if etag and response_etag and str(response_etag).strip('"') != etag.strip('"'):
        close = getattr(body, "close", None)
        if close:
            close()
        raise ValueError("S3 object ETag changed before image repair")
    try:
        while chunk := body.read(1024 * 1024):
            target.write(chunk)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()


def _verified_existing_entry(
    path: Path, *, expected_sha256: str | None, expected_size: int | None
) -> CacheEntry:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    sha256 = digest.hexdigest()
    if expected_size is not None and size != expected_size:
        raise ValueError("cached image size drifted")
    if expected_sha256 is not None and sha256 != expected_sha256.lower():
        raise ValueError("cached image checksum drifted")
    return CacheEntry(path=path, size=size, sha256=sha256)
