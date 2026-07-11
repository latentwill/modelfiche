from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .credentials import SourceCredentials, SourceCredentialsError

if TYPE_CHECKING:
    from titles_api.integrations.config import S3Settings


class SourceClientError(RuntimeError):
    pass


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


__all__ = ["SourceClientError", "SourceCredentialsError", "create_source_s3_client"]
