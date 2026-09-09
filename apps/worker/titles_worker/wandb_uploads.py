from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.storage.s3_client import create_source_s3_client
from titles_api.training_metrics import append_run_event
from titles_api.wandb_uploads import source_settings


ClientFactory = Callable[[models.ImportSource], Any]


def _client_for_source(source: models.ImportSource) -> Any:
    return create_source_s3_client(source_settings(source))


def _is_missing_object(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return isinstance(error, dict) and str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"}


def _body_digest(body: Any) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    return size, digest.hexdigest()


def _fingerprint(source: models.ImportSource, upload: models.RunUpload, digest: str) -> str:
    value = f"{source.id}\0{upload.object_key}\0{upload.version_id or upload.etag or ''}\0{digest}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fail_upload(
    session: Session,
    run: models.TrainingRun,
    upload: models.RunUpload,
    message: str,
) -> None:
    upload.state = "failed"
    upload.error = message
    append_run_event(
        session,
        run,
        type="run.upload.failed",
        idempotency_key=f"wandb:upload:failed:{upload.id}:{upload.generation}:{message}",
        payload={"upload_id": upload.id, "relative_path": upload.relative_path, "error": message},
        step=upload.step,
    )


def _materialize_sample(
    session: Session,
    run: models.TrainingRun,
    launch: models.TrainingLaunch,
    source: models.ImportSource,
    upload: models.RunUpload,
    digest: str,
    size: int,
) -> None:
    if upload.kind != "image":
        return
    metadata = dict(upload.metadata_ or {})
    location = session.scalar(
        select(models.AssetLocation)
        .where(
            models.AssetLocation.provider == "s3",
            models.AssetLocation.source_id == source.id,
            models.AssetLocation.bucket == source.bucket,
            models.AssetLocation.object_key == upload.object_key,
        )
        .order_by(models.AssetLocation.last_seen_at.desc(), models.AssetLocation.updated_at.desc())
        .limit(1)
    )
    if location is None:
        location = session.scalar(
            select(models.AssetLocation)
            .where(
                models.AssetLocation.provider == "s3",
                models.AssetLocation.bucket == source.bucket,
                models.AssetLocation.object_key == upload.object_key,
                models.AssetLocation.etag == upload.etag,
            )
            .order_by(models.AssetLocation.last_seen_at.desc(), models.AssetLocation.updated_at.desc())
            .limit(1)
        )
    asset = location.asset if location is not None else None
    if asset is None and upload.asset_id:
        asset = session.get(models.Asset, upload.asset_id)
    if asset is None:
        asset = models.Asset(
            workspace_id=launch.workspace_id,
            project_id=run.project_id,
            kind=models.AssetKind.image,
            name=Path(upload.relative_path).name,
            mime_type=upload.mime_type,
            sha256=digest,
            provenance_kind="wandb",
            metadata_={
                "origin": "wandb",
                "run_upload_id": upload.id,
                "metric_name": upload.metric_name,
            },
        )
        session.add(asset)
        session.flush()
    else:
        asset.workspace_id = launch.workspace_id
        if asset.project_id is None:
            asset.project_id = run.project_id
        asset.sha256 = digest
        if upload.mime_type:
            asset.mime_type = upload.mime_type
        asset.metadata_ = {
            **dict(asset.metadata_ or {}),
            "origin": "wandb",
            "run_upload_id": upload.id,
            "metric_name": upload.metric_name,
        }
    upload.asset_id = asset.id
    now = models.utcnow()
    if location is None:
        location = models.AssetLocation(
            asset_id=asset.id,
            workspace_id=launch.workspace_id,
            provider="s3",
            uri=f"s3://{source.bucket}/{upload.object_key}",
            bucket=source.bucket,
            object_key=upload.object_key,
            etag=upload.etag,
            version_id=upload.version_id,
            source_id=source.id,
            source_revision_fingerprint=_fingerprint(source, upload, digest),
            size=size,
            verified_size=size,
            verified_sha256=digest,
            verification_state="verified",
            last_verified_at=now,
            hydration_state="remote",
        )
        session.add(location)
        session.flush()
    else:
        location.asset_id = asset.id
        location.workspace_id = launch.workspace_id
        location.uri = f"s3://{source.bucket}/{upload.object_key}"
        location.bucket = source.bucket
        location.object_key = upload.object_key
        location.source_id = source.id
        location.source_revision_fingerprint = _fingerprint(source, upload, digest)
        location.etag = upload.etag
        location.version_id = upload.version_id
        location.size = size
        location.verified_size = size
        location.verified_sha256 = digest
        location.verification_state = "verified"
        location.last_verified_at = now
        location.last_seen_at = now
        location.hydration_state = "remote"
    asset.preferred_location_id = location.id
    asset.origin_location_id = asset.origin_location_id or location.id
    image_metadata = session.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
    if image_metadata is None:
        image_metadata = models.ImageMetadata(
            asset_id=asset.id,
            width=metadata.get("width") if isinstance(metadata.get("width"), int) else None,
            height=metadata.get("height") if isinstance(metadata.get("height"), int) else None,
            color_mode=None,
            orientation=None,
            exif_summary={},
            perceptual_hash=None,
        )
        session.add(image_metadata)
    samples = session.scalars(select(models.Sample).where(models.Sample.run_id == run.id)).all()
    sample = next(
        (
            candidate
            for candidate in samples
            if (candidate.generation_metadata or {}).get("run_upload_id") == upload.id
        ),
        None,
    )
    if sample is None:
        sample = models.Sample(
            run_id=run.id,
            asset_id=asset.id,
            step=upload.step,
            prompt=upload.caption,
            generation_metadata={
                "origin": "wandb",
                "run_upload_id": upload.id,
                "metric_name": upload.metric_name,
                "upload_state": "verified",
            },
            modified_at=now,
        )
        session.add(sample)
    else:
        sample.asset_id = asset.id
        sample.step = upload.step
        sample.prompt = upload.caption
        sample.modified_at = now
        sample.generation_metadata = {
            **dict(sample.generation_metadata or {}),
            "metric_name": upload.metric_name,
            "upload_state": "verified",
        }
    append_run_event(
        session,
        run,
        type="run.sample.ready",
        idempotency_key=f"wandb:sample:ready:{upload.id}:{upload.generation}:{digest}",
        payload={
            "upload_id": upload.id,
            "asset_id": asset.id,
            "sample_id": sample.id,
            "caption": upload.caption,
        },
        step=upload.step,
    )


class WandbUploadReconciler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        client_factory: ClientFactory = _client_for_source,
    ) -> None:
        self.session_factory = session_factory
        self.client_factory = client_factory

    def run_once(self, *, limit: int = 50) -> int:
        with self.session_factory() as session:
            upload_ids = list(
                session.scalars(
                    select(models.RunUpload.id)
                    .where(models.RunUpload.state.in_(("pending", "uploaded", "verifying")))
                    .order_by(models.RunUpload.updated_at, models.RunUpload.id)
                    .limit(limit)
                )
            )
        completed = 0
        for upload_id in upload_ids:
            try:
                if self._reconcile(upload_id):
                    completed += 1
            except Exception as exc:
                self._record_transient_error(upload_id, exc)
        return completed

    def _reconcile(self, upload_id: str) -> bool:
        # Phase 1: short transaction to validate and prepare. No remote I/O
        # while a row lock or write transaction is held.
        with self.session_factory.begin() as session:
            upload = session.get(models.RunUpload, upload_id, with_for_update=True)
            if upload is None or upload.state not in {"pending", "uploaded", "verifying"}:
                return False
            run = session.get(models.TrainingRun, upload.run_id)
            launch = session.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == upload.run_id))
            source = session.get(models.ImportSource, launch.source_id) if launch and launch.source_id else None
            if run is None or launch is None or source is None or not source.is_active:
                return False
            if upload.kind == "image" and upload.expected_sha256 is None:
                return False
            client = self.client_factory(source)
            state = upload.state
            staged_value = (upload.metadata_ or {}).get("staged_path")

        staged_unlink: Path | None = None
        if state == "uploaded" and isinstance(staged_value, str):
            staged_path = Path(staged_value)
            if not staged_path.is_file():
                with self.session_factory.begin() as session:
                    upload = session.get(models.RunUpload, upload_id, with_for_update=True)
                    run = session.get(models.TrainingRun, upload.run_id)
                    _fail_upload(session, run, upload, "staged upload is unavailable")
                return True
            with staged_path.open("rb") as body:
                client.put_object(
                    Bucket=source.bucket,
                    Key=upload.object_key,
                    Body=body,
                    **({"ContentType": upload.mime_type} if upload.mime_type else {}),
                )
            staged_unlink = staged_path

        # Remote verification runs with no open database transaction.
        try:
            head = client.head_object(Bucket=source.bucket, Key=upload.object_key)
        except Exception as exc:
            if _is_missing_object(exc):
                return False
            raise
        etag = str(head.get("ETag", "")).strip('"') or None
        version_id = head.get("VersionId")
        actual_head_size = head.get("ContentLength")
        response = client.get_object(Bucket=source.bucket, Key=upload.object_key)
        size, digest = _body_digest(response["Body"])

        # Phase 2: short transaction to apply results to the still-valid row.
        with self.session_factory.begin() as session:
            upload = session.get(models.RunUpload, upload_id, with_for_update=True)
            if upload is None or upload.state not in {"pending", "uploaded", "verifying"}:
                return False
            run = session.get(models.TrainingRun, upload.run_id)
            launch = session.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == upload.run_id))
            source = session.get(models.ImportSource, launch.source_id) if launch and launch.source_id else None
            if run is None or launch is None or source is None or not source.is_active:
                return False
            metadata = dict(upload.metadata_ or {})
            upload.etag = etag or upload.etag
            upload.version_id = version_id or upload.version_id
            if upload.expected_size is not None and actual_head_size != upload.expected_size:
                _fail_upload(session, run, upload, "uploaded object size does not match W&B metadata")
                return True
            if upload.expected_size is not None and size != upload.expected_size:
                _fail_upload(session, run, upload, "uploaded object size does not match W&B metadata")
                return True
            if upload.expected_sha256 is not None and digest != upload.expected_sha256:
                _fail_upload(session, run, upload, "uploaded object digest does not match W&B metadata")
                return True
            upload.expected_size = size
            upload.expected_sha256 = digest
            upload.state = "verified"
            upload.error = None
            upload.metadata_ = {
                **metadata,
                "bucket": source.bucket,
                "upload_mode": "s3",
                "verified_at": models.utcnow().isoformat(),
            }
            _materialize_sample(session, run, launch, source, upload, digest, size)
            append_run_event(
                session,
                run,
                type="run.upload.verified",
                idempotency_key=f"wandb:upload:verified:{upload.id}:{upload.generation}:{digest}",
                payload={
                    "upload_id": upload.id,
                    "relative_path": upload.relative_path,
                    "size": size,
                    "asset_id": upload.asset_id,
                },
                step=upload.step,
            )
        if staged_unlink is not None:
            staged_unlink.unlink(missing_ok=True)
        return True

    def _record_transient_error(self, upload_id: str, exc: Exception) -> None:
        with self.session_factory.begin() as session:
            upload = session.get(models.RunUpload, upload_id, with_for_update=True)
            if upload is None or upload.state in {"verified", "failed"}:
                return
            upload.metadata_ = {
                **dict(upload.metadata_ or {}),
                "last_reconcile_error": type(exc).__name__,
                "last_reconcile_at": models.utcnow().isoformat(),
            }


__all__ = ["WandbUploadReconciler"]
