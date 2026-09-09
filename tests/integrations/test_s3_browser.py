import pytest
import sys
from types import SimpleNamespace

from titles_api.integrations.config import S3Settings
from titles_api.integrations.s3.browser import PrefixAccessError, S3Browser


class FakeClient:
    def list_objects_v2(self, **kwargs):
        return {"Contents": [], "CommonPrefixes": [], "IsTruncated": False}


def test_browser_rejects_bucket_root_and_parent_traversal():
    browser = S3Browser(FakeClient(), S3Settings("https://s3.invalid", "bucket", ("allowed/root/",)))
    with pytest.raises(PrefixAccessError):
        browser.browse("")
    with pytest.raises(PrefixAccessError):
        browser.browse("allowed/root/../secret")


def test_browser_accepts_only_configured_subtree():
    browser = S3Browser(FakeClient(), S3Settings("https://s3.invalid", "bucket", ("allowed/root/",)))
    assert browser.browse("allowed/root/child/").prefix == "allowed/root/child/"
    with pytest.raises(PrefixAccessError):
        browser.browse("allowed/root-secret/")


def test_standard_aws_source_requires_named_credentials_before_client_creation(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda **kwargs: calls.append(kwargs) or FakeClient()))
    settings = S3Settings(None, "bucket", ("allowed/",), region="ap-northeast-1")
    with pytest.raises(ValueError, match="S3_ACCESS_KEY and S3_SECRET_KEY"):
        S3Browser.from_settings(settings)
    assert calls == []

def test_generic_s3_environment_precedes_legacy_mega(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "aws-bucket")
    monkeypatch.setenv("S3_ALLOWED_PREFIXES", "training/")
    monkeypatch.setenv("MEGA_BUCKET", "mega-bucket")
    monkeypatch.setenv("MEGA_ALLOWED_PREFIXES", "legacy/")
    settings = S3Settings.from_env()
    assert settings.bucket == "aws-bucket"
    assert settings.endpoint_url is None
    assert settings.credential_env_prefix == "S3"


def test_legacy_mega_environment_remains_supported(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("MEGA_BUCKET", "mega-bucket")
    monkeypatch.setenv("MEGA_ALLOWED_PREFIXES", "legacy/")
    settings = S3Settings.from_env()
    assert settings.bucket == "mega-bucket"
    assert settings.endpoint_url == "https://s3.g.s4.mega.io"
    assert settings.credential_env_prefix == "MEGA"


class ChangedEtagBody:
    def __init__(self):
        self._chunks = [b"replacement", b""]

    def read(self, _size):
        return self._chunks.pop(0)

    def close(self):
        return None


class ChangedEtagClient:
    def get_object(self, **_kwargs):
        return {"ETag": '"replacement"', "Body": ChangedEtagBody()}


def test_download_rejects_a_response_that_does_not_match_the_requested_etag():
    browser = S3Browser(ChangedEtagClient(), S3Settings("https://s3.invalid", "bucket", ("allowed/",)))

    with pytest.raises(ValueError, match="ETag changed"):
        browser.download("allowed/checkpoint", SimpleNamespace(write=lambda _chunk: None), etag="expected")
