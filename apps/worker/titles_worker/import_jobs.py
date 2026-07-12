from __future__ import annotations

from typing import Any
import json
import mimetypes
from pathlib import PurePosixPath
import tarfile

from boto3.s3.transfer import TransferConfig

from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import or_, select

from titles_api import models
from titles_api.asset_cache import AssetCache
from titles_api.storage.repository import StorageRepository
from titles_api.integrations.config import S3Settings
from titles_api.integrations.s3.browser import S3Browser
from titles_api.integrations.s3.importer import ImportContext, S3Importer
from titles_api.integrations.s3.sqlalchemy_sink import SQLAlchemyImportSink
from titles_api.models import ImportSource

from .runner import JobContext


class _NonSeekableReader:
    def __init__(self, stream):
        self.stream = stream

    def read(self, size: int = -1):
        return self.stream.read(size)

    def seekable(self) -> bool:
        return False


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
        except Exception:
            pass
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
                        except Exception:
                            pass
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
        with self.session_factory() as session:
            remote_locations = list(session.scalars(
                select(models.AssetLocation)
                .join(models.Asset, models.Asset.id == models.AssetLocation.asset_id)
                .where(
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
                .order_by(models.AssetLocation.object_key)
            ))

        hydrated_count = 0
        hydrated_bytes = 0
        total = max(len(remote_locations), 1)
        for index, remote in enumerate(remote_locations):
            if context.cancellation_requested():
                return {"count": hydrated_count, "bytes": hydrated_bytes}
            with self.session_factory() as session:
                existing = session.scalar(select(models.AssetLocation).where(
                    models.AssetLocation.asset_id == remote.asset_id,
                    models.AssetLocation.provider == "local",
                    models.AssetLocation.hydration_state == "hydrated",
                ))
                asset = session.get(models.Asset, remote.asset_id)
                expected_sha256 = asset.sha256 if asset else None
            if existing:
                context.progress(0.25 + 0.7 * ((index + 1) / total))
                continue
            if not remote.object_key:
                raise ValueError(f"image asset {remote.asset_id} has no S3 object key")
            object_key = browser.require_allowed(remote.object_key)
            expected_size = remote.size
            if expected_size is None:
                head = browser.client.head_object(Bucket=settings.bucket, Key=object_key)
                expected_size = int(head.get("ContentLength", 0))

            def download(stream) -> None:
                browser.download(object_key, stream, etag=remote.etag)
                if context.cancellation_requested():
                    raise RuntimeError("image hydration canceled")

            try:
                entry = self.cache.hydrate(expected_size, download, expected_sha256=expected_sha256)
            except RuntimeError:
                if context.cancellation_requested():
                    return {"count": hydrated_count, "bytes": hydrated_bytes}
                raise
            with self.session_factory.begin() as session:
                asset = session.get(models.Asset, remote.asset_id)
                if asset is None:
                    raise LookupError(f"image asset was removed during hydration: {remote.asset_id}")
                StorageRepository(session, asset.workspace_id).attach_verified_local_location(
                    asset_id=asset.id,
                    uri=str(entry.path),
                    size=entry.size,
                    sha256=entry.sha256,
                    mime_type=asset.mime_type or "application/octet-stream",
                )
            hydrated_count += 1
            hydrated_bytes += entry.size
            context.progress(0.25 + 0.7 * ((index + 1) / total))
        return {"count": hydrated_count, "bytes": hydrated_bytes}
