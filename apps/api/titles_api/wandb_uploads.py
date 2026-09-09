from __future__ import annotations

import mimetypes
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .integrations.config import S3Settings
from .storage.credentials import SourceCredentialsError
from .storage.s3_client import SourceClientError, create_source_s3_client
from .training_metrics import append_run_event


PRESIGN_TTL_SECONDS = 600

class UploadError(ValueError):
    pass


def safe_relative_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise UploadError("invalid W&B upload path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise UploadError("invalid W&B upload path")
    normalized = path.as_posix()
    if len(normalized) > 1024:
        raise UploadError("W&B upload path is too long")
    return normalized


def source_settings(source: models.ImportSource) -> S3Settings:
    return S3Settings.for_source(
        endpoint_url=source.endpoint_url,
        bucket=source.bucket,
        allowed_prefixes=tuple(source.allowed_prefixes or ()),
        region=source.region,
        addressing_style=source.addressing_style,
        credential_env_prefix=source.credential_env_prefix,
    )


def get_or_create_upload(
    session: Session,
    run: models.TrainingRun,
    launch: models.TrainingLaunch,
    relative_path_value: str,
) -> models.RunUpload:
    relative_path = safe_relative_path(relative_path_value)
    upload = session.scalar(
        select(models.RunUpload).where(
            models.RunUpload.run_id == run.id,
            models.RunUpload.relative_path == relative_path,
        )
    )
    if upload is not None:
        return upload
    image_path = relative_path.startswith("media/images/") and PurePosixPath(relative_path).suffix.casefold() in {
        ".jpeg",
        ".jpg",
        ".png",
        ".webp",
    }
    config_path = relative_path in {
        "config.yaml",
        "requirements.txt",
        "wandb-metadata.json",
        "wandb-summary.json",
    }
    if not image_path and not config_path:
        raise UploadError(f"unsupported W&B file upload: {relative_path}")
    kind = "image" if image_path else "config"
    object_relative_path = (
        f"samples/{PurePosixPath(relative_path).name}"
        if image_path
        else f"configs/{relative_path}"
    )
    upload = models.RunUpload(
        run_id=run.id,
        relative_path=relative_path,
        kind=kind,
        object_key=f"{launch.run_prefix}{object_relative_path}",
        state="pending",
        mime_type=mimetypes.guess_type(relative_path)[0],
        metadata_={"protocol": "wandb-0.28.0"},
    )
    session.add(upload)
    session.flush()
    return upload


def upload_instruction(
    session: Session,
    run: models.TrainingRun,
    launch: models.TrainingLaunch,
    relative_path: str,
    *,
    fallback_base_url: str,
) -> tuple[models.RunUpload, str]:
    upload = get_or_create_upload(session, run, launch, relative_path)
    now = models.utcnow()
    if upload.state in {"failed", "verified"} or (
        upload.state == "pending"
        and upload.presign_expires_at is not None
        and upload.presign_expires_at <= now
    ):
        upload.generation += 1
        upload.state = "pending"
        upload.presign_expires_at = None
        upload.etag = None
        upload.version_id = None
        upload.error = None
    source = session.get(models.ImportSource, launch.source_id) if launch.source_id else None
    if source is not None and source.is_active:
        try:
            settings = source_settings(source)
            client = create_source_s3_client(settings)
            url = client.generate_presigned_url(
                "put_object",
                Params={"Bucket": source.bucket, "Key": upload.object_key},
                ExpiresIn=PRESIGN_TTL_SECONDS,
            )
            upload.presign_expires_at = now + timedelta(seconds=PRESIGN_TTL_SECONDS)
            upload.metadata_ = {**dict(upload.metadata_ or {}), "upload_mode": "s3"}
            upload.error = None
            return upload, str(url)
        except (SourceCredentialsError, SourceClientError, ValueError):
            pass
    upload.metadata_ = {**dict(upload.metadata_ or {}), "upload_mode": "staged"}
    return upload, f"{fallback_base_url.rstrip('/')}/wandb-upload/{upload.id}?generation={upload.generation}"


def capture_media_metadata(
    session: Session,
    run: models.TrainingRun,
    launch: models.TrainingLaunch,
    row: dict[str, Any],
) -> list[models.RunUpload]:
    step_value = row.get("_step", run.current_step)
    step = step_value if isinstance(step_value, int) and not isinstance(step_value, bool) and step_value >= 0 else run.current_step
    captured: list[models.RunUpload] = []
    for metric_name, media in row.items():
        if not isinstance(metric_name, str) or not isinstance(media, dict) or media.get("_type") != "image-file":
            continue
        path_value = media.get("path")
        if not isinstance(path_value, str):
            raise UploadError("W&B image metadata is missing its media path")
        upload = get_or_create_upload(session, run, launch, path_value)
        upload.kind = "image"
        upload.metric_name = metric_name[:120]
        upload.step = step
        caption = media.get("caption")
        upload.caption = caption if isinstance(caption, str) else None
        digest = media.get("sha256")
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
            raise UploadError("W&B image metadata has an invalid SHA-256 digest")
        size = media.get("size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise UploadError("W&B image metadata has an invalid size")
        upload.expected_sha256 = digest
        upload.expected_size = size
        image_format = media.get("format")
        if isinstance(image_format, str):
            upload.mime_type = f"image/{image_format.casefold()}"
        upload.metadata_ = {
            **dict(upload.metadata_ or {}),
            "width": media.get("width"),
            "height": media.get("height"),
            "format": image_format,
            "wandb_media": media,
        }
        append_run_event(
            session,
            run,
            type="run.sample.pending",
            idempotency_key=f"wandb:sample:pending:{run.id}:{upload.relative_path}:{digest or 'unknown'}",
            payload={
                "upload_id": upload.id,
                "metric_name": upload.metric_name,
                "relative_path": upload.relative_path,
                "caption": upload.caption,
                "expected_size": upload.expected_size,
            },
            step=step,
        )
        captured.append(upload)
    return captured


__all__ = [
    "PRESIGN_TTL_SECONDS",
    "UploadError",
    "capture_media_metadata",
    "get_or_create_upload",
    "upload_instruction",
    "source_settings",
    "safe_relative_path",
]
