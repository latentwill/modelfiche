from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.integrations.config import S3Settings
from titles_api.integrations.fal.adapters import LoraInput, get_adapter
from titles_api.integrations.s3.browser import S3Browser


FAL_HANDOFF_TTL_SECONDS = 600


def _validate_handoff_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("FAL checkpoint handoff URL is missing")
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("FAL checkpoint handoff URL must be public HTTPS")
    return value


class SQLAlchemyFalRequestResolver:
    """Build a provider request from a versioned, admitted checkpoint handoff."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        checkpoint_id = str(payload["checkpoint_id"])
        with self.session_factory() as session:
            checkpoint = session.get(models.Checkpoint, checkpoint_id)
            if not checkpoint:
                raise LookupError("checkpoint not found")
            path = self._fal_handoff_url(session, checkpoint, payload)
        adapter = get_adapter(str(payload["endpoint_id"]))
        parameters = dict(payload.get("parameters") or {})
        parameters.pop("endpoint_id", None)
        scale = float(parameters.pop("lora_scale", payload.get("lora_scale", 1.0)))
        return adapter.build_request(
            prompt=str(payload["prompt"]),
            loras=[LoraInput(path, scale)],
            parameters=parameters,
            grid_cell=bool(payload.get("grid_cell", False)),
        )

    def _fal_handoff_url(self, session: Session, checkpoint: models.Checkpoint, payload: dict[str, Any]) -> str:
        versions = session.scalars(
            select(models.ModelVersion).where(models.ModelVersion.checkpoint_id == checkpoint.id)
        ).all()
        if len(versions) > 1:
            raise ValueError("checkpoint has ambiguous model-version FAL configuration")
        if versions:
            readiness = versions[0].readiness or {}
            if readiness.get("fal_url") is not None:
                return _validate_handoff_url(readiness.get("fal_url"))
        handoff = payload.get("checkpoint_handoff")
        if isinstance(handoff, dict):
            url = _validate_handoff_url(handoff.get("url"))
            expires_at = handoff.get("expires_at")
            if isinstance(expires_at, str):
                try:
                    expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("FAL checkpoint handoff expiry is invalid") from exc
                if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                    raise ValueError("FAL checkpoint handoff has expired")
            elif expires_at is not None:
                raise ValueError("FAL checkpoint handoff expiry is invalid")
            return url
        asset = session.get(models.Asset, checkpoint.asset_id)
        preferred_location_id = asset.preferred_location_id if asset else None

        location = session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.asset_id == checkpoint.asset_id,
                models.AssetLocation.verification_state == "available",
                models.AssetLocation.hydration_state != "remote_missing",
            ).order_by(
                models.AssetLocation.id == preferred_location_id,
                models.AssetLocation.provider,
            )
        )
        if not location:
            raise ValueError("checkpoint has no verified asset location")
        if location.provider != "s3" or not location.bucket or not location.object_key or not location.version_id:
            raise ValueError("checkpoint requires an exact versioned S3 handoff")
        source = session.scalar(
            select(models.ImportSource).where(
                models.ImportSource.id == location.source_id,
                models.ImportSource.is_active.is_(True),
            )
        )
        if not source:
            raise ValueError("checkpoint S3 source is unavailable")
        settings = S3Settings.for_source(
            endpoint_url=source.endpoint_url,
            bucket=source.bucket,
            allowed_prefixes=tuple(source.allowed_prefixes),
            region=source.region,
            addressing_style=source.addressing_style,
            credential_env_prefix=source.credential_env_prefix,
        )
        browser = S3Browser.from_settings(settings)
        browser.require_allowed(location.object_key)
        return browser.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": location.bucket, "Key": location.object_key, "VersionId": location.version_id},
            ExpiresIn=FAL_HANDOFF_TTL_SECONDS,
        )
