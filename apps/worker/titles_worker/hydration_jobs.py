from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.asset_cache import AssetCache
from titles_api.checkpoint_revisions import establish_checkpoint_revision
from titles_api.settings import get_settings
from titles_api.storage.repository import StorageRepository
from titles_api.integrations.config import S3Settings
from titles_api.integrations.s3.browser import S3Browser

from .runner import JobContext


def _usable_local_path(uri: str) -> bool:
    path = Path(uri.removeprefix("file://")).expanduser().resolve()
    settings = get_settings()
    roots = (settings.asset_root.resolve(), settings.cache_root.resolve())
    return (
        any(path == root or root in path.parents for root in roots)
        and path.is_file()
    )


class CheckpointHydrationHandler:
    def __init__(self, session_factory: sessionmaker[Session], cache: AssetCache):
        self.session_factory = session_factory
        self.cache = cache

    def __call__(self, context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        checkpoint_id = str(payload["checkpoint_id"])
        with self.session_factory() as session:
            checkpoint = session.get(models.Checkpoint, checkpoint_id)
            if checkpoint is None:
                raise LookupError("checkpoint not found")
            asset = session.get(models.Asset, checkpoint.asset_id)
            if asset is None:
                raise LookupError("checkpoint asset not found")
            existing_locations = list(session.scalars(select(models.AssetLocation).where(
                models.AssetLocation.asset_id == asset.id,
                models.AssetLocation.provider == "local",
                models.AssetLocation.hydration_state == "hydrated",
            ).order_by(models.AssetLocation.created_at.desc())))
            existing = next(
                (location for location in existing_locations if _usable_local_path(location.uri)),
                None,
            )
            if existing:
                establish_checkpoint_revision(session, checkpoint, location=existing, force_new=True)
                session.commit()
                return {"checkpoint_id": checkpoint.id, "asset_id": asset.id, "path": existing.uri, "already_hydrated": True}
            remote = session.scalar(select(models.AssetLocation).where(
                models.AssetLocation.asset_id == asset.id,
                models.AssetLocation.provider == "s3",
                models.AssetLocation.hydration_state != "remote_missing",
            ).order_by(models.AssetLocation.created_at.desc()))
            if remote is None or not remote.bucket or not remote.object_key:
                raise LookupError("checkpoint has no available S3 location")
            source = session.scalar(
                select(models.ImportSource)
                .where(
                    models.ImportSource.is_active.is_(True),
                    models.ImportSource.bucket == remote.bucket,
                    models.ImportSource.id == remote.source_id,
                )
                .order_by(models.ImportSource.created_at.desc())
            )
            if source is None:
                source = session.scalar(
                    select(models.ImportSource)
                    .where(
                        models.ImportSource.is_active.is_(True),
                        models.ImportSource.bucket == remote.bucket,
                    )
                    .order_by(models.ImportSource.created_at.desc())
                )
            if source is None:
                raise LookupError("no active import source is available for the checkpoint bucket")
            settings = S3Settings.for_source(
                endpoint_url=source.endpoint_url,
                bucket=source.bucket,
                allowed_prefixes=tuple(source.allowed_prefixes),
                region=source.region,
                addressing_style=source.addressing_style,
                credential_env_prefix=source.credential_env_prefix,
            )
            expected_size = remote.size if remote.size is not None else remote.verified_size
            object_key = remote.object_key
            expected_etag = remote.etag
            expected_version_id = remote.version_id
            expected_sha256 = asset.sha256 or remote.verified_sha256
        if context.cancellation_requested():
            return {"checkpoint_id": checkpoint_id, "canceled": True}
        browser = S3Browser.from_settings(settings)
        browser.require_allowed(object_key)
        if expected_size is None:
            head_request = {"Bucket": settings.bucket, "Key": object_key}
            if expected_etag:
                head_request["IfMatch"] = expected_etag
            if expected_version_id:
                head_request["VersionId"] = expected_version_id
            expected_size = int(browser.client.head_object(**head_request).get("ContentLength", 0))

        def download(stream) -> None:
            request = {"Bucket": settings.bucket, "Key": object_key}
            if expected_etag:
                request["IfMatch"] = expected_etag
            if expected_version_id:
                request["VersionId"] = expected_version_id
            response = browser.client.get_object(**request)
            body = response["Body"]
            try:
                while chunk := body.read(1024 * 1024):
                    if context.cancellation_requested():
                        raise RuntimeError("checkpoint hydration canceled")
                    stream.write(chunk)
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
        context.progress(0.1)
        try:
            entry = self.cache.persist(
                self.cache.hydrate(expected_size, download, expected_sha256=expected_sha256),
                get_settings().asset_root,
            )
        except RuntimeError:
            if context.cancellation_requested():
                return {"checkpoint_id": checkpoint_id, "canceled": True}
            raise
        context.progress(0.9)
        with self.session_factory.begin() as session:
            checkpoint = session.get(models.Checkpoint, checkpoint_id)
            if checkpoint is None:
                raise LookupError("checkpoint was removed during hydration")
            asset = session.get(models.Asset, checkpoint.asset_id)
            if asset is None:
                raise LookupError("checkpoint asset was removed during hydration")
            location = StorageRepository(session, asset.workspace_id).attach_verified_local_location(
                asset_id=asset.id,
                uri=str(entry.path),
                size=entry.size,
                sha256=entry.sha256,
                mime_type=asset.mime_type or "application/octet-stream",
            )
            establish_checkpoint_revision(session, checkpoint, location=location, force_new=True)
            checkpoint.state = "hydrated"
        return {"checkpoint_id": checkpoint_id, "asset_id": asset.id, "path": str(entry.path), "size": entry.size, "sha256": entry.sha256}
