from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sys

import pytest

from titles_api.integrations.config import S3Settings
from titles_api.storage.credentials import SourceCredentials, SourceCredentialsError
from titles_api.storage.s3_client import create_source_s3_client


def source_settings() -> S3Settings:
    return S3Settings(
        endpoint_url="https://s3.example.test",
        bucket="images",
        allowed_prefixes=("managed/",),
        credential_env_prefix="IMAGE_STORE",
    )


def test_source_settings_ignore_ambient_aws_environment(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "bucket")
    monkeypatch.setenv("S3_ALLOWED_PREFIXES", "assets/")
    monkeypatch.setenv("AWS_REGION", "ambient-region")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ambient-default")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "ambient-access")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "ambient-secret")

    settings = S3Settings.from_env("S3", legacy_fallback=False)

    assert settings.region is None
    assert settings.access_key_id is None
    assert settings.secret_access_key is None


def test_source_settings_do_not_require_allowed_prefixes(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "bucket")
    monkeypatch.delenv("S3_ALLOWED_PREFIXES", raising=False)

    settings = S3Settings.from_env("S3", legacy_fallback=False)

    assert settings.bucket == "bucket"
    assert settings.allowed_prefixes == ()


def test_missing_named_credentials_stops_before_sdk_construction(monkeypatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda **kwargs: calls.append(kwargs)))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("AWS_EC2_METADATA_SERVICE_ENDPOINT", "http://169.254.169.254")

    with pytest.raises(SourceCredentialsError, match="IMAGE_STORE_ACCESS_KEY"):
        create_source_s3_client(source_settings())

    assert calls == []


def test_source_client_uses_only_exact_named_tuple(monkeypatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda **kwargs: calls.append(kwargs) or object()))
    monkeypatch.setenv("IMAGE_STORE_ACCESS_KEY", "named-access")
    monkeypatch.setenv("IMAGE_STORE_SECRET_KEY", "named-secret")
    monkeypatch.setenv("IMAGE_STORE_SESSION_TOKEN", "named-token")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient")

    create_source_s3_client(source_settings())

    assert len(calls) == 1
    assert calls[0]["aws_access_key_id"] == "named-access"
    assert calls[0]["aws_secret_access_key"] == "named-secret"
    assert calls[0]["aws_session_token"] == "named-token"
    assert calls[0]["endpoint_url"] == "https://s3.example.test"
    assert calls[0]["config"].proxies == {}


def test_credential_epoch_changes_for_every_tuple_component_and_expiry():
    now = datetime.now(timezone.utc)
    baseline = SourceCredentials("P", "a", "s", "t", now + timedelta(hours=1))
    variants = [
        SourceCredentials("P", "b", "s", "t", baseline.expires_at),
        SourceCredentials("P", "a", "x", "t", baseline.expires_at),
        SourceCredentials("P", "a", "s", None, baseline.expires_at),
        SourceCredentials("P", "a", "s", "t", now + timedelta(hours=2)),
    ]
    assert len({baseline.epoch, *(item.epoch for item in variants)}) == 5
    assert not baseline.is_expired(now)
    assert SourceCredentials("P", "a", "s", expires_at=now).is_expired(now)


def test_legacy_access_key_id_alias_is_not_a_named_credential():
    with pytest.raises(SourceCredentialsError):
        SourceCredentials.from_environ(
            "SOURCE",
            {"SOURCE_ACCESS_KEY_ID": "legacy", "SOURCE_SECRET_ACCESS_KEY": "legacy"},
        )
