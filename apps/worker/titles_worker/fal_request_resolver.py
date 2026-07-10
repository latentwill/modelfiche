from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.integrations.config import S3Settings
from titles_api.integrations.fal.adapters import LoraInput, get_adapter
from titles_api.integrations.s3.browser import S3Browser


class SQLAlchemyFalRequestResolver:
    """Build a provider request at execution time so signed URLs are never stored."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        checkpoint_id = str(payload["checkpoint_id"])
        with self.session_factory() as session:
            checkpoint = session.get(models.Checkpoint, checkpoint_id)
            if not checkpoint:
                raise LookupError("checkpoint not found")
            path = self._fal_path(session, checkpoint)
        adapter = get_adapter(str(payload["endpoint_id"]))
        parameters = dict(payload.get("parameters") or {})
        scale = float(parameters.pop("lora_scale", payload.get("lora_scale", 1.0)))
        return adapter.build_request(
            prompt=str(payload["prompt"]),
            loras=[LoraInput(path, scale)],
            parameters=parameters,
            grid_cell=bool(payload.get("grid_cell", False)),
        )

    def _fal_path(self, session: Session, checkpoint: models.Checkpoint) -> str:
        version = session.scalar(select(models.ModelVersion).where(models.ModelVersion.checkpoint_id == checkpoint.id))
        if version and version.readiness.get("fal_path"):
            return str(version.readiness["fal_path"])
        location = session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.asset_id == checkpoint.asset_id,
                models.AssetLocation.hydration_state != "remote_missing",
            ).order_by(models.AssetLocation.provider)
        )
        if not location:
            raise ValueError("checkpoint has no available asset location")
        if location.uri.startswith(("https://", "http://")):
            return location.uri
        if location.provider == "s3" and location.bucket and location.object_key:
            source = session.scalar(
                select(models.ImportSource).where(
                    models.ImportSource.bucket == location.bucket,
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
            preference = session.scalar(select(models.LocalPreference).where(models.LocalPreference.key == "operator_settings"))
            raw_ttl = ((preference.value.get("storage") or {}).get("signed_url_ttl") if preference and isinstance(preference.value, dict) else None)
            ttl = max(60, min(86_400, int(raw_ttl or 3600)))
            return browser.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": location.bucket, "Key": location.object_key},
                ExpiresIn=ttl,
            )
        raise ValueError("checkpoint is not FAL-accessible; hydrate it or set readiness.fal_path")
