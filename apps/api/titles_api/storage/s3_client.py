from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from .credentials import SourceCredentials, SourceCredentialsError

if TYPE_CHECKING:
    from titles_api.integrations.config import S3Settings


class SourceClientError(RuntimeError):
    pass

@lru_cache(maxsize=128)
def _cached_source_s3_client(
    endpoint_url: str | None,
    region: str | None,
    addressing_style: str,
    credential_env_prefix: str,
) -> Any:
    from titles_api.integrations.config import S3Settings

    settings = S3Settings.for_source(
        endpoint_url=endpoint_url,
        bucket="",
        region=region,
        addressing_style=addressing_style,
        credential_env_prefix=credential_env_prefix,
    )
    return create_source_s3_client(settings)


def cached_source_s3_client(settings: "S3Settings") -> Any:
    """Return one S3 client for each immutable source connection settings."""
    return _cached_source_s3_client(
        settings.endpoint_url,
        settings.region,
        settings.addressing_style,
        settings.credential_env_prefix,
    )


def create_source_s3_client(
    settings: "S3Settings",
    *,
    credentials: SourceCredentials | None = None,
) -> Any:
    """Create an S3 client using only credentials named by this immutable source.

    Credentials are loaded before importing or constructing the SDK client, so a
    missing tuple cannot trigger proxy, profile, or instance-metadata traffic.
    """
    resolved = credentials or SourceCredentials.from_environ(settings.credential_env_prefix)
    if resolved.env_prefix != settings.credential_env_prefix.upper():
        raise SourceClientError("credentials do not belong to this source")

    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=10,
        read_timeout=30,
        retries={"mode": "standard", "max_attempts": 3},
        proxies={},
        max_pool_connections=32,
        tcp_keepalive=True,
        s3={"addressing_style": settings.addressing_style},
    )
    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "region_name": settings.region or "us-east-1",
        "config": config,
        "aws_access_key_id": resolved.access_key,
        "aws_secret_access_key": resolved.secret_key,
    }
    if settings.endpoint_url:
        kwargs["endpoint_url"] = settings.endpoint_url
    if resolved.session_token:
        kwargs["aws_session_token"] = resolved.session_token
    return boto3.client(**kwargs)


__all__ = ["SourceClientError", "SourceCredentialsError", "create_source_s3_client", "cached_source_s3_client"]
