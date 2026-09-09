from __future__ import annotations

from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import hashlib
import mimetypes
import os
from pathlib import Path, PurePosixPath
import tarfile
from threading import local

from botocore.exceptions import ClientError
from boto3.s3.transfer import TransferConfig

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.asset_cache import AssetCache, CacheEntry
from titles_api.storage.repository import StorageRepository
from titles_api.integrations.config import S3Settings
from titles_api.integrations.s3.browser import S3Browser
from titles_api.integrations.s3.importer import ImportContext, S3Importer
from titles_api.integrations.s3.sqlalchemy_sink import SQLAlchemyImportSink
from titles_api.image_provenance import extract_image_metadata
from titles_api.models import ImportSource
from titles_api.settings import get_settings



def _is_missing_object(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}
from .runner import JobContext


class _NonSeekableReader:
    def __init__(self, stream):
        self.stream = stream

    def read(self, size: int = -1):
        return self.stream.read(size)

    def seekable(self) -> bool:
        return False


def _hydration_worker_count() -> int:
    raw = os.environ.get("TITLES_HYDRATION_WORKERS")
    if raw is None:
        return 4
    try:
        workers = int(raw)
    except ValueError as exc:
        raise ValueError("TITLES_HYDRATION_WORKERS must be a positive integer") from exc
    if workers < 1:
        raise ValueError("TITLES_HYDRATION_WORKERS must be a positive integer")
    return workers


class S3ImportHandler:
    def __init__(self, session_factory: sessionmaker[Session], cache: AssetCache):
        self.session_factory = session_factory
        self.cache = cache

    def __call__(self, context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        with self.session_factory() as session:
            source = session.get(ImportSource, str(payload["source_id"]))
            if not source:
                raise LookupError("import source not found")
            settings = S3Settings.for_source(
                endpoint_url=source.endpoint_url,
                bucket=source.bucket,
                allowed_prefixes=tuple(source.allowed_prefixes),
                region=source.region,
                addressing_style=source.addressing_style,
                credential_env_prefix=source.credential_env_prefix,
            )
            browser = S3Browser.from_settings(settings)
            prefix = str(payload["prefix"])
            source_id = source.id
        # Archive expansion can take many minutes. Do not retain even a read
        # transaction while streaming S3 objects: SQLite schema/startup writes
        # and caption reads must remain available throughout the transfer.
        if prefix.lower().endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
            return self._import_archive(context, payload, browser, settings, prefix)
        with self.session_factory() as session:
            importer = S3Importer(browser, SQLAlchemyImportSink(session))
            context.progress(0.02)
            if bool(payload.get("dry_run", False)):
                inventory, detection = importer.preview(str(payload["prefix"]))
                requested_kind = payload.get("kind")
                if requested_kind and detection.kind.value != requested_kind:
                    raise ValueError(
                        f"selected prefix detected as {detection.kind.value}, not requested {requested_kind}"
                    )
                context.progress(0.98)
                return {
                    "dry_run": True,
                    "detection": {
                        "kind": detection.kind.value,
                        "confidence": detection.confidence,
                        "signals": list(detection.signals),
                        "warnings": list(detection.warnings),
                        "observed": detection.observed,
                        "declared": detection.declared,
                        "metadata": detection.metadata,
                    },
                    "indexed": 0,
                    "would_index": len(inventory.current()),
                    "warnings": list(detection.warnings),
                }
            result = importer.run(
                ImportContext(
                    source_id=source_id,
                    project_id=str(payload["project_id"]),
                    prefix=str(payload["prefix"]),
                    requested_by_profile_id=payload.get("profile_id"),
                    hydrate_dataset_images=bool(payload.get("hydrate_dataset_images", True)),
                )
            )
        hydrated = {"count": 0, "bytes": 0}
        if bool(payload.get("hydrate_dataset_images", True)):
            hydrated = self._hydrate_images(context, browser, settings, str(payload["prefix"]))
        context.progress(0.98)
        return {**result.as_json(), "hydrated_images": hydrated}

    def _import_archive(self, context: JobContext, payload: dict[str, Any], browser: S3Browser, settings: S3Settings, archive_key: str) -> dict[str, Any]:
        safe_key = browser.require_allowed(archive_key)
        head = browser.client.head_object(Bucket=settings.bucket, Key=safe_key)
        expected_size = int(head.get("ContentLength", 0))
        etag = str(head.get("ETag", "")).strip('"')
        archive_name = PurePosixPath(safe_key).name
        for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tar"):
            if archive_name.lower().endswith(suffix):
                archive_name = archive_name[:-len(suffix)]
                break
        expanded_prefix = f"runpod-expanded/{archive_name}/"
        marker_key = expanded_prefix + "_titles_archive_import.json"
        already_expanded = False
        try:
            marker = browser.client.get_object(Bucket=settings.bucket, Key=marker_key)["Body"].read()
            already_expanded = json.loads(marker).get("archive_etag") == etag
        except ClientError as exc:
            if not _is_missing_object(exc):
                raise
        if not already_expanded:
            response = browser.client.get_object(Bucket=settings.bucket, Key=safe_key, IfMatch=etag)
            stream = response["Body"]
            uploaded = 0
            try:
                with tarfile.open(fileobj=stream, mode="r|*") as archive:
                    for member in archive:
                        path = PurePosixPath(member.name)
                        if not member.isfile() or path.is_absolute() or ".." in path.parts:
                            continue
                        if context.cancellation_requested():
                            raise RuntimeError("archive expansion canceled")
                        lower_name = path.name.lower()
                        if lower_name in {"optimizer.pt", "scheduler.pt", "rng_state.pth", "scaler.pt"} or lower_name.endswith((".db-wal", ".db-shm")):
                            continue
                        body = archive.extractfile(member)
                        if body is None:
                            continue
                        destination = expanded_prefix + str(path)
                        try:
                            existing = browser.client.head_object(Bucket=settings.bucket, Key=destination)
                            existing_metadata = existing.get("Metadata", {})
                            if int(existing.get("ContentLength", -1)) == member.size and existing_metadata.get("titles-archive-etag") == etag:
                                uploaded += 1
                                continue
                        except ClientError as exc:
                            if not _is_missing_object(exc):
                                raise
                        extra = {"Metadata": {"titles-archive-key": safe_key, "titles-archive-etag": etag}}
                        content_type = mimetypes.guess_type(str(path))[0]
                        if content_type:
                            extra["ContentType"] = content_type
                        browser.client.upload_fileobj(_NonSeekableReader(body), settings.bucket, destination, ExtraArgs=extra, Config=TransferConfig(use_threads=False))
                        uploaded += 1
                        context.progress(min(0.7, 0.02 + 0.65 * (stream.tell() / max(expected_size, 1))))
            finally:
                stream.close()
            browser.client.put_object(Bucket=settings.bucket, Key=marker_key, Body=json.dumps({"archive_key": safe_key, "archive_etag": etag, "objects": uploaded}).encode(), ContentType="application/json")
        context.progress(0.72)
        if bool(payload.get("dry_run", False)):
            with self.session_factory() as session:
                importer = S3Importer(browser, SQLAlchemyImportSink(session))
                inventory, detection = importer.preview(expanded_prefix)
            return {"dry_run": True, "archive_key": safe_key, "expanded_prefix": expanded_prefix, "would_index": len(inventory.current()), "detection": {"kind": detection.kind.value, "confidence": detection.confidence, "signals": list(detection.signals), "warnings": list(detection.warnings), "observed": detection.observed, "declared": detection.declared, "metadata": detection.metadata}}
        with self.session_factory() as session:
            importer = S3Importer(browser, SQLAlchemyImportSink(session))
            result = importer.run(ImportContext(source_id=str(payload["source_id"]), project_id=str(payload["project_id"]), prefix=expanded_prefix, requested_by_profile_id=payload.get("profile_id"), hydrate_dataset_images=bool(payload.get("hydrate_dataset_images", True))))
            import_job = session.scalar(select(models.ImportJob).where(models.ImportJob.source_id == str(payload["source_id"]), models.ImportJob.project_id == str(payload["project_id"]), models.ImportJob.prefix == safe_key).order_by(models.ImportJob.created_at.desc()))
            if import_job:
                import_job.detected_type = result.detection.kind.value
                import_job.state = "succeeded"
                import_job.progress = 1
                import_job.warnings = [{"message": warning} for warning in result.warnings]
                import_job.result = {**result.as_json(), "archive_key": safe_key, "expanded_prefix": expanded_prefix}
            session.commit()
        hydrated = self._hydrate_images(context, browser, settings, expanded_prefix) if bool(payload.get("hydrate_dataset_images", True)) else {"count": 0, "bytes": 0}
        context.progress(0.98)
        return {**result.as_json(), "archive_key": safe_key, "expanded_prefix": expanded_prefix, "hydrated_images": hydrated}

    def _hydrate_images(
        self,
        context: JobContext,
        browser: S3Browser,
        settings: S3Settings,
        prefix: str,
    ) -> dict[str, int]:
        safe_prefix = browser.require_allowed(prefix)
        filters = (
            models.AssetLocation.provider == "s3",
            models.AssetLocation.bucket == settings.bucket,
            or_(
                models.AssetLocation.object_key == safe_prefix.rstrip("/"),
                models.AssetLocation.object_key.startswith(safe_prefix.rstrip("/") + "/"),
            ),
            models.AssetLocation.hydration_state != "remote_missing",
            models.Asset.kind == models.AssetKind.image,
            models.Asset.metadata_["category"].as_string().in_(["dataset_image", "sample"]),
        )
        chunk_size = 500
        with self.session_factory() as session:
            total = max(session.scalar(select(func.count()).select_from(models.AssetLocation).join(
                models.Asset, models.Asset.id == models.AssetLocation.asset_id).where(*filters)) or 0, 1)

        hydrated_count = 0
        hydrated_bytes = 0
        completed_count = 0
        last_key: str | None = None
        worker_count = _hydration_worker_count()
        browser_local = local()
        local_sources: dict[str, Path] = {}

        def hydrate_remote(remote: models.AssetLocation, expected_sha256: str | None) -> Any:
            local_path = local_sources.get(remote.asset_id)
            if local_path is not None:
                size = local_path.stat().st_size
                if remote.size is not None and size != remote.size:
                    raise ValueError(f"local image size mismatch: expected {remote.size}, received {size}")
                digest = hashlib.sha256()
                with local_path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                sha256 = digest.hexdigest()
                if expected_sha256 and sha256 != expected_sha256.lower():
                    raise ValueError("local image checksum mismatch")
                entry = CacheEntry(local_path, size, sha256)
                return self.cache.persist(entry, get_settings().asset_root)

            worker_browser = getattr(browser_local, "browser", None)
            if worker_browser is None:
                worker_browser = S3Browser.from_settings(settings)
                browser_local.browser = worker_browser
            object_key = worker_browser.require_allowed(remote.object_key)
            expected_size = remote.size
            if expected_size is None:
                head = worker_browser.client.head_object(Bucket=settings.bucket, Key=object_key)
                expected_size = int(head.get("ContentLength", 0))

            def download(stream) -> None:
                worker_browser.download(object_key, stream, etag=remote.etag)
                if context.cancellation_requested():
                    raise RuntimeError("image hydration canceled")

            entry = self.cache.hydrate(expected_size, download, expected_sha256=expected_sha256)
            return self.cache.persist(entry, get_settings().asset_root)

        def attach(remote: models.AssetLocation, entry: Any) -> None:
            nonlocal hydrated_count, hydrated_bytes
            image_metadata = extract_image_metadata(entry.path.read_bytes())
            with self.session_factory.begin() as session:
                asset = session.get(models.Asset, remote.asset_id)
                if asset is None:
                    raise LookupError(f"image asset was removed during hydration: {remote.asset_id}")
                location = StorageRepository(session, asset.workspace_id).attach_verified_local_location(
                    asset_id=asset.id,
                    uri=str(entry.path),
                    size=entry.size,
                    sha256=entry.sha256,
                    mime_type=asset.mime_type or "application/octet-stream",
                )
                location.relative_path = str(entry.path.relative_to(get_settings().asset_root.resolve()))
                if image_metadata:
                    merged = dict(asset.metadata_ or {})
                    for key, value in image_metadata.items():
                        merged.setdefault(key, value)
                    asset.metadata_ = merged
                    persisted = session.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
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
                    caption = str(image_metadata.get("caption") or "").strip()
                    generation = image_metadata.get("generation_metadata")
                    if isinstance(generation, dict):
                        samples = session.scalars(
                            select(models.Sample).where(models.Sample.asset_id == asset.id)
                        )
                        for sample in samples:
                            if caption and not sample.prompt:
                                sample.prompt = caption
                            sample.generation_metadata = {
                                **generation,
                                **dict(sample.generation_metadata or {}),
                            }
            hydrated_count += 1
            hydrated_bytes += entry.size

        def complete(futures: dict[Any, tuple[models.AssetLocation, bool]]) -> bool:
            nonlocal completed_count
            for future in as_completed(futures):
                remote, skipped = futures[future]
                try:
                    entry = future.result()
                except RuntimeError:
                    if context.cancellation_requested():
                        for pending in futures:
                            pending.cancel()
                        return True
                    raise
                if context.cancellation_requested():
                    for pending in futures:
                        pending.cancel()
                    return True
                if not skipped:
                    attach(remote, entry)
                completed_count += 1
                context.progress(0.25 + 0.7 * (completed_count / total))
            return False

        def dispatch(executor: ThreadPoolExecutor, batch: list[tuple[models.AssetLocation, str | None, bool]]) -> bool:
            if context.cancellation_requested():
                return True
            futures = {}
            for item, expected_sha256, skipped in batch:
                if skipped:
                    future = executor.submit(lambda: None)
                else:
                    future = executor.submit(hydrate_remote, item, expected_sha256)
                futures[future] = (item, skipped)
            return complete(futures)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            while True:
                conditions = list(filters)
                if last_key is not None:
                    conditions.append(models.AssetLocation.object_key > last_key)
                with self.session_factory() as session:
                    chunk = list(session.scalars(
                        select(models.AssetLocation)
                        .join(models.Asset, models.Asset.id == models.AssetLocation.asset_id)
                        .where(*conditions)
                        .order_by(models.AssetLocation.object_key)
                        .limit(chunk_size)
                    ))
                if not chunk:
                    break
                last_key = chunk[-1].object_key
                batch: list[tuple[models.AssetLocation, str | None, bool]] = []
                for remote in chunk:
                    if context.cancellation_requested():
                        return {"count": hydrated_count, "bytes": hydrated_bytes}
                    with self.session_factory() as session:
                        existing = session.scalar(select(models.AssetLocation).where(
                            models.AssetLocation.asset_id == remote.asset_id,
                            models.AssetLocation.provider == "local",
                            models.AssetLocation.hydration_state == "hydrated",
                        ))
                        asset = session.get(models.Asset, remote.asset_id)
                        image_metadata = session.scalar(
                            select(models.ImageMetadata).where(
                                models.ImageMetadata.asset_id == remote.asset_id
                            )
                        )
                        expected_sha256 = asset.sha256 if asset else None
                    local_path = (
                        Path(existing.uri.removeprefix("file://"))
                        if existing and Path(existing.uri.removeprefix("file://")).is_file()
                        else None
                    )
                    if local_path is not None and image_metadata is None:
                        local_sources[remote.asset_id] = local_path
                    skipped = bool(local_path is not None and image_metadata is not None)
                    if not skipped and not remote.object_key and remote.asset_id not in local_sources:
                        raise ValueError(f"image asset {remote.asset_id} has no S3 object key")
                    batch.append((remote, expected_sha256, skipped))
                    if len(batch) < worker_count:
                        continue
                    if dispatch(executor, batch):
                        return {"count": hydrated_count, "bytes": hydrated_bytes}
                    batch.clear()
                if batch:
                    if dispatch(executor, batch):
                        return {"count": hydrated_count, "bytes": hydrated_bytes}
        return {"count": hydrated_count, "bytes": hydrated_bytes}
