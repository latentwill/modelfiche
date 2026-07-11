from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io

import pytest

from titles_api.storage.capabilities import (
    CapabilityBinding,
    CapabilityCache,
    CapabilityKind,
    ProbeBounds,
    S3CapabilityProber,
)


class S3Error(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, *, fake_versions: bool = False, ignore_if_match: bool = False):
        self.fake_versions = fake_versions
        self.ignore_if_match = ignore_if_match
        self.objects: dict[str, list[dict[str, object]]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.counter = 0

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, **_kwargs):
        if IfNoneMatch == "*" and self.objects.get(Key):
            raise S3Error("PreconditionFailed")
        self.counter += 1
        data = bytes(Body)
        version = "fake" if self.fake_versions else f"v{self.counter}"
        item = {"Body": data, "ETag": f'"etag-{self.counter}"', "VersionId": version}
        self.objects.setdefault(Key, []).append(item)
        return {"ETag": item["ETag"], "VersionId": version}

    def get_object(self, *, Bucket, Key, VersionId=None, IfMatch=None, **_kwargs):
        versions = self.objects.get(Key, [])
        if not versions:
            raise S3Error("NoSuchKey")
        if VersionId is None:
            item = versions[-1]
        else:
            matches = [entry for entry in versions if entry["VersionId"] == VersionId]
            if not matches and not self.fake_versions:
                raise S3Error("NoSuchVersion")
            item = matches[-1] if matches else versions[-1]
        if IfMatch and not self.ignore_if_match and IfMatch.strip('"') != str(item["ETag"]).strip('"'):
            raise S3Error("PreconditionFailed")
        return {
            "Body": io.BytesIO(item["Body"]),
            "ETag": item["ETag"],
            "VersionId": item["VersionId"],
            "ContentLength": len(item["Body"]),
        }

    def get_object_acl(self, *, Bucket, Key, VersionId=None):
        return {"Grants": [{"Grantee": {"Type": "CanonicalUser"}, "Permission": "FULL_CONTROL"}]}

    def delete_object(self, *, Bucket, Key, VersionId=None, **_kwargs):
        if not VersionId:
            raise AssertionError("unqualified delete")
        before = list(self.objects.get(Key, []))
        self.objects[Key] = [entry for entry in before if entry["VersionId"] != VersionId]
        self.deleted.append((Key, VersionId))
        return {"VersionId": VersionId}


def binding(**changes) -> CapabilityBinding:
    values = {
        "source_fingerprint": "source-v1:abc",
        "credential_epoch": "credential-v1:def",
        "adapter_version": "s3-probe-v1",
        "process_boot_id": "boot-1",
        "bounds_fingerprint": ProbeBounds().fingerprint,
    }
    values.update(changes)
    return CapabilityBinding(**values)


def test_positive_capability_is_current_for_fifteen_minutes_only():
    now = datetime.now(timezone.utc)
    cache = CapabilityCache()
    result = cache.record(CapabilityKind.VERSIONED_READ, binding(), supported=True, now=now, evidence_identity="proof")
    assert result.expires_at == now + timedelta(minutes=15)
    assert cache.current(CapabilityKind.VERSIONED_READ, binding(), now=now + timedelta(minutes=14)) == result
    assert cache.current(CapabilityKind.VERSIONED_READ, binding(), now=now + timedelta(minutes=15)) is None


@pytest.mark.parametrize("field,value", [
    ("credential_epoch", "credential-v1:rotated"),
    ("source_fingerprint", "source-v1:replacement"),
    ("adapter_version", "s3-probe-v2"),
    ("process_boot_id", "boot-2"),
    ("bounds_fingerprint", "bounds-v1:changed"),
])
def test_binding_changes_invalidate_cached_capability(field, value):
    now = datetime.now(timezone.utc)
    cache = CapabilityCache()
    cache.record(CapabilityKind.CONDITIONAL_READ, binding(), supported=True, now=now, evidence_identity="proof")
    assert cache.current(CapabilityKind.CONDITIONAL_READ, binding(**{field: value}), now=now) is None


def test_fake_version_ids_fail_closed_and_cleanup_is_never_unqualified():
    client = FakeS3(fake_versions=True)
    result = S3CapabilityProber(client, bucket="b", probe_prefix="probe/", binding=binding()).probe(CapabilityKind.VERSIONED_READ)
    assert not result.supported
    assert result.failure_category == "version_semantics_untrustworthy"
    assert all(version for _, version in client.deleted)


def test_provider_ignoring_old_if_match_fails_closed():
    client = FakeS3(ignore_if_match=True)
    result = S3CapabilityProber(client, bucket="b", probe_prefix="probe/", binding=binding()).probe(CapabilityKind.CONDITIONAL_READ)
    assert not result.supported
    assert result.failure_category == "conditional_read_unsafe"


def test_versioned_write_and_exact_cleanup_probe_succeeds():
    client = FakeS3()
    prober = S3CapabilityProber(client, bucket="b", probe_prefix="probe/", binding=binding())
    assert prober.probe(CapabilityKind.VERSIONED_READ).supported
    assert prober.probe(CapabilityKind.MANAGED_WRITE).supported
    assert prober.probe(CapabilityKind.CLEANUP).supported
    assert client.deleted and all(version for _, version in client.deleted)


def test_probe_refuses_oversized_configured_payload_before_s3_call():
    client = FakeS3()
    bounds = ProbeBounds(max_probe_bytes=4, probe_payload_bytes=8)
    prober = S3CapabilityProber(client, bucket="b", probe_prefix="probe/", binding=binding(bounds_fingerprint=bounds.fingerprint), bounds=bounds)
    with pytest.raises(ValueError, match="bound"):
        prober.probe(CapabilityKind.MANAGED_WRITE)
    assert client.objects == {}
