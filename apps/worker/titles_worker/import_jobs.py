from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import or_, select

from titles_api import models
from titles_api.asset_cache import AssetCache
from titles_api.integrations.config import S3Settings
from titles_api.integrations.s3.browser import S3Browser
from titles_api.integrations.s3.importer import ImportContext, S3Importer
from titles_api.integrations.s3.sqlalchemy_sink import SQLAlchemyImportSink
from titles_api.models import ImportSource

from .runner import JobContext


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
                    source_id=source.id,
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
                local = session.scalar(select(models.AssetLocation).where(
                    models.AssetLocation.asset_id == asset.id,
                    models.AssetLocation.provider == "local",
                    models.AssetLocation.uri == str(entry.path),
                ))
                if local is None:
                    session.add(models.AssetLocation(
                        asset_id=asset.id,
                        provider="local",
                        uri=str(entry.path),
                        size=entry.size,
                        hydration_state="hydrated",
                    ))
                else:
                    local.size = entry.size
                    local.hydration_state = "hydrated"
                asset.sha256 = entry.sha256
            hydrated_count += 1
            hydrated_bytes += entry.size
            context.progress(0.25 + 0.7 * ((index + 1) / total))
        return {"count": hydrated_count, "bytes": hydrated_bytes}
