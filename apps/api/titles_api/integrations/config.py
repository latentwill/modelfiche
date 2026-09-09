from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip().lstrip("/") for part in (value or "").split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class S3Settings:
    endpoint_url: str | None
    bucket: str
    allowed_prefixes: tuple[str, ...]
    region: str | None = None
    addressing_style: str = "auto"
    credential_env_prefix: str = "S3"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None

    @classmethod
    def from_env(cls, prefix: str | None = None, *, legacy_fallback: bool = True) -> "S3Settings":
        selected = (prefix or "S3").upper()
        if selected == "S3" and legacy_fallback and not os.getenv("S3_BUCKET") and os.getenv("MEGA_BUCKET"):
            selected = "MEGA"
        endpoint = os.getenv(f"{selected}_ENDPOINT")
        if selected == "MEGA" and not endpoint:
            endpoint = "https://s3.g.s4.mega.io"
        bucket = os.getenv(f"{selected}_BUCKET", "")
        prefixes = _csv(os.getenv(f"{selected}_ALLOWED_PREFIXES"))
        if not bucket:
            raise ValueError(f"{selected}_BUCKET is required")
        settings = cls(
            endpoint_url=endpoint.rstrip("/") if endpoint else None,
            bucket=bucket,
            allowed_prefixes=prefixes,
            region=os.getenv(f"{selected}_REGION"),
            addressing_style=os.getenv(f"{selected}_ADDRESSING_STYLE", "auto"),
            credential_env_prefix=selected,
            access_key_id=os.getenv(f"{selected}_ACCESS_KEY"),
            secret_access_key=os.getenv(f"{selected}_SECRET_KEY"),
            session_token=os.getenv(f"{selected}_SESSION_TOKEN"),
        )
        if settings.addressing_style not in {"auto", "path", "virtual"}:
            raise ValueError(f"{selected}_ADDRESSING_STYLE must be auto, path, or virtual")
        if bool(settings.access_key_id) != bool(settings.secret_access_key):
            raise ValueError(f"{selected} credentials require both access and secret keys")
        return settings

    @classmethod
    def for_source(
        cls,
        *,
        endpoint_url: str | None,
        bucket: str,
        allowed_prefixes: tuple[str, ...] = (),
        region: str | None = None,
        addressing_style: str = "auto",
        credential_env_prefix: str = "S3",
    ) -> "S3Settings":
        prefix = credential_env_prefix.upper()
        settings = cls(
            endpoint_url=endpoint_url.rstrip("/") if endpoint_url else None,
            bucket=bucket,
            allowed_prefixes=allowed_prefixes,
            region=region,
            addressing_style=addressing_style,
            credential_env_prefix=prefix,
            access_key_id=os.getenv(f"{prefix}_ACCESS_KEY"),
            secret_access_key=os.getenv(f"{prefix}_SECRET_KEY"),
            session_token=os.getenv(f"{prefix}_SESSION_TOKEN"),
        )
        if settings.addressing_style not in {"auto", "path", "virtual"}:
            raise ValueError("S3 addressing_style must be auto, path, or virtual")
        if bool(settings.access_key_id) != bool(settings.secret_access_key):
            raise ValueError(f"{prefix} credentials require both access and secret keys")
        return settings


@dataclass(frozen=True, slots=True)
class CacheSettings:
    root: Path
    max_bytes: int = 20 * 1024**3
    thumbnail_sizes: tuple[int, ...] = (256, 512, 1024)

    @classmethod
    def from_env(cls) -> "CacheSettings":
        return cls(
            root=Path(os.getenv("TITLES_CACHE_DIR", "var/cache")).expanduser().resolve(),
            max_bytes=int(os.getenv("TITLES_CACHE_MAX_BYTES", str(20 * 1024**3))),
        )


@dataclass(frozen=True, slots=True)
class FalSettings:
    api_key: str
    base_url: str = "https://queue.fal.run"

    @classmethod
    def from_env(cls) -> "FalSettings":
        from .fal.credentials import resolve_fal_credential

        credential = resolve_fal_credential()
        if not credential:
            raise ValueError("FAL credential is not configured")
        return cls(api_key=credential.key, base_url=os.getenv("FAL_QUEUE_URL", "https://queue.fal.run").rstrip("/"))
