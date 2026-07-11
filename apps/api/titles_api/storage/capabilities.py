from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable


POSITIVE_TTL = timedelta(minutes=15)
PROBE_ADAPTER_VERSION = "s3-capability-v1"


class CapabilityKind(StrEnum):
    BASIC_READ = "basic_read"
    CONDITIONAL_READ = "conditional_read"
    VERSIONED_READ = "versioned_read"
    MANAGED_WRITE = "managed_write"
    CLEANUP = "cleanup"
    LOCALLY_VERIFIED_HANDOFF = "locally_verified_handoff"
    PROVIDER_VERIFIED_HANDOFF = "provider_verified_handoff"


@dataclass(frozen=True, slots=True)
class ProbeBounds:
    max_probe_bytes: int = 1024
    probe_payload_bytes: int = 32

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            {"max_probe_bytes": self.max_probe_bytes, "probe_payload_bytes": self.probe_payload_bytes},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"probe-bounds-v1:{hashlib.sha256(encoded).hexdigest()}"

    def validate(self) -> None:
        if not 1 <= self.probe_payload_bytes <= self.max_probe_bytes <= 1024:
            raise ValueError("probe payload exceeds the 1 KiB bound")


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    source_fingerprint: str
    credential_epoch: str
    adapter_version: str
    process_boot_id: str
    bounds_fingerprint: str

    @property
    def identity(self) -> str:
        encoded = "\0".join((
            self.source_fingerprint,
            self.credential_epoch,
            self.adapter_version,
            self.process_boot_id,
            self.bounds_fingerprint,
        )).encode()
        return f"capability-binding-v1:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    kind: CapabilityKind
    available: bool
    checked_at: datetime
    expires_at: datetime | None
    evidence_identity: str | None = None
    failure_code: str | None = None

    @property
    def supported(self) -> bool:
        return self.available

    @property
    def failure_category(self) -> str | None:
        return self.failure_code


class CapabilityCache:
    """Process-local replay cache; persisted rows may mirror results but never supply credential epochs."""

    def __init__(self) -> None:
        self._results: dict[tuple[CapabilityKind, str], CapabilityResult] = {}

    def record(
        self,
        kind: CapabilityKind,
        binding: CapabilityBinding,
        *,
        supported: bool,
        now: datetime | None = None,
        evidence_identity: str | None = None,
        failure_category: str | None = None,
    ) -> CapabilityResult:
        checked = now or datetime.now(timezone.utc)
        result = CapabilityResult(
            kind=kind,
            available=supported,
            checked_at=checked,
            expires_at=checked + POSITIVE_TTL if supported else None,
            evidence_identity=evidence_identity if supported else None,
            failure_code=None if supported else (failure_category or "probe_failed"),
        )
        self._results[(kind, binding.identity)] = result
        return result

    def current(
        self,
        kind: CapabilityKind,
        binding: CapabilityBinding,
        *,
        now: datetime | None = None,
    ) -> CapabilityResult | None:
        result = self._results.get((kind, binding.identity))
        if result is None or not result.available or result.expires_at is None:
            return None
        if result.expires_at <= (now or datetime.now(timezone.utc)):
            return None
        return result


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and error.get("Code"):
            return str(error["Code"])
    return exc.__class__.__name__


def _is_precondition_failure(exc: BaseException) -> bool:
    return _error_code(exc).lower() in {"preconditionfailed", "412", "conditionalrequestconflict"}


def _body_bytes(response: dict[str, Any], maximum: int) -> bytes:
    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise ValueError("missing_body")
    value = body.read(maximum + 1)
    if not isinstance(value, (bytes, bytearray)) or len(value) > maximum:
        raise ValueError("body_bound")
    return bytes(value)


def _etag(response: dict[str, Any]) -> str:
    value = str(response.get("ETag") or "").strip('"')
    if not value:
        raise ValueError("missing_etag")
    return value


def _version(response: dict[str, Any]) -> str:
    value = str(response.get("VersionId") or "").strip()
    if not value or value.lower() in {"null", "none"}:
        raise ValueError("missing_version")
    return value


class S3CapabilityProber:
    """Destructive probes limited to a caller-owned probe prefix and exact-version cleanup."""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        probe_prefix: str = "titles-dam/probes/",
        binding: CapabilityBinding | None = None,
        bounds: ProbeBounds | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.bucket = bucket
        self.probe_prefix = probe_prefix.strip("/") + "/"
        self.bounds = bounds or ProbeBounds()
        self.binding = binding
        self._now = now or (lambda: datetime.now(timezone.utc))

    def probe(self, kind: CapabilityKind) -> CapabilityResult:
        self.bounds.validate()
        methods = {
            CapabilityKind.CONDITIONAL_READ: self.probe_conditional_read,
            CapabilityKind.VERSIONED_READ: self.probe_versioned_read,
            CapabilityKind.MANAGED_WRITE: self.probe_managed_write,
            CapabilityKind.CLEANUP: self.probe_cleanup,
        }
        method = methods.get(kind)
        if method is None:
            return self._result(kind, False, failure="probe_not_implemented")
        return method(self._new_key(kind.value))

    def _new_key(self, label: str) -> str:
        return f"{self.probe_prefix}{label}-{secrets.token_hex(16)}"

    def _result(
        self,
        kind: CapabilityKind,
        available: bool,
        *,
        evidence: str | None = None,
        failure: str | None = None,
    ) -> CapabilityResult:
        checked = self._now()
        return CapabilityResult(
            kind=kind,
            available=available,
            checked_at=checked,
            expires_at=checked + POSITIVE_TTL if available else None,
            evidence_identity=evidence if available else None,
            failure_code=None if available else failure,
        )

    def _cleanup_versions(self, key: str, versions: list[str]) -> None:
        for version in dict.fromkeys(versions):
            if version:
                try:
                    self.client.delete_object(Bucket=self.bucket, Key=key, VersionId=version)
                except Exception:
                    pass

    def probe_conditional_read(self, key: str) -> CapabilityResult:
        self.bounds.validate()
        versions: list[str] = []
        first = b"A" * self.bounds.probe_payload_bytes
        second = b"B" * self.bounds.probe_payload_bytes
        try:
            put_a = self.client.put_object(Bucket=self.bucket, Key=key, Body=first)
            if put_a.get("VersionId"):
                versions.append(str(put_a["VersionId"]))
            etag_a = _etag(put_a)
            response_a = self.client.get_object(Bucket=self.bucket, Key=key, IfMatch=etag_a)
            if _body_bytes(response_a, self.bounds.max_probe_bytes) != first:
                raise ValueError("first_read_mismatch")
            put_b = self.client.put_object(Bucket=self.bucket, Key=key, Body=second)
            if put_b.get("VersionId"):
                versions.append(str(put_b["VersionId"]))
            try:
                stale = self.client.get_object(Bucket=self.bucket, Key=key, IfMatch=etag_a)
                _body_bytes(stale, self.bounds.max_probe_bytes)
            except Exception as exc:
                if _is_precondition_failure(exc):
                    evidence = hashlib.sha256((etag_a + _etag(put_b)).encode()).hexdigest()
                    return self._result(CapabilityKind.CONDITIONAL_READ, True, evidence=f"conditional-v1:{evidence}")
                raise
            return self._result(CapabilityKind.CONDITIONAL_READ, False, failure="conditional_read_unsafe")
        except Exception:
            return self._result(CapabilityKind.CONDITIONAL_READ, False, failure="conditional_read_unsafe")
        finally:
            self._cleanup_versions(key, versions)

    def probe_versioned_read(self, key: str) -> CapabilityResult:
        self.bounds.validate()
        versions: list[str] = []
        first = b"A" * self.bounds.probe_payload_bytes
        second = b"B" * self.bounds.probe_payload_bytes
        try:
            put_a = self.client.put_object(Bucket=self.bucket, Key=key, Body=first)
            version_a = _version(put_a)
            versions.append(version_a)
            put_b = self.client.put_object(Bucket=self.bucket, Key=key, Body=second)
            version_b = _version(put_b)
            versions.append(version_b)
            if version_a == version_b:
                raise ValueError("same_version")
            read_a = self.client.get_object(Bucket=self.bucket, Key=key, VersionId=version_a)
            read_b = self.client.get_object(Bucket=self.bucket, Key=key, VersionId=version_b)
            if _version(read_a) != version_a or _version(read_b) != version_b:
                raise ValueError("version_echo_mismatch")
            if _body_bytes(read_a, self.bounds.max_probe_bytes) != first or _body_bytes(read_b, self.bounds.max_probe_bytes) != second:
                raise ValueError("version_body_mismatch")
            evidence = hashlib.sha256((version_a + "\0" + version_b).encode()).hexdigest()
            return self._result(CapabilityKind.VERSIONED_READ, True, evidence=f"versioned-v1:{evidence}")
        except Exception:
            return self._result(CapabilityKind.VERSIONED_READ, False, failure="version_semantics_untrustworthy")
        finally:
            self._cleanup_versions(key, versions)

    def probe_managed_write(self, key: str) -> CapabilityResult:
        self.bounds.validate()
        versions: list[str] = []
        payload = secrets.token_bytes(self.bounds.probe_payload_bytes)
        try:
            put = self.client.put_object(Bucket=self.bucket, Key=key, Body=payload, IfNoneMatch="*")
            version = _version(put)
            versions.append(version)
            try:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=b"collision", IfNoneMatch="*")
            except Exception as exc:
                if not _is_precondition_failure(exc):
                    raise
            else:
                raise ValueError("create_only_ignored")
            read = self.client.get_object(Bucket=self.bucket, Key=key, VersionId=version)
            if _version(read) != version or _body_bytes(read, self.bounds.max_probe_bytes) != payload:
                raise ValueError("readback_mismatch")
            grants = self.client.get_object_acl(Bucket=self.bucket, Key=key, VersionId=version).get("Grants", [])
            if any(
                grant.get("Grantee", {}).get("Type") != "CanonicalUser"
                or grant.get("Permission") not in {"FULL_CONTROL", "READ_ACP", "WRITE_ACP"}
                for grant in grants
            ):
                raise ValueError("not_private")
            evidence = hashlib.sha256(payload).hexdigest()
            return self._result(CapabilityKind.MANAGED_WRITE, True, evidence=f"managed-write-v1:{evidence}")
        except Exception:
            return self._result(CapabilityKind.MANAGED_WRITE, False, failure="managed_write_unsafe")
        finally:
            self._cleanup_versions(key, versions)

    def probe_cleanup(self, key: str) -> CapabilityResult:
        self.bounds.validate()
        versions: list[str] = []
        first = b"old" + secrets.token_bytes(self.bounds.probe_payload_bytes)
        replacement = b"new" + secrets.token_bytes(self.bounds.probe_payload_bytes)
        try:
            old_version = _version(self.client.put_object(Bucket=self.bucket, Key=key, Body=first))
            versions.append(old_version)
            new_version = _version(self.client.put_object(Bucket=self.bucket, Key=key, Body=replacement))
            versions.append(new_version)
            self.client.delete_object(Bucket=self.bucket, Key=key, VersionId=old_version)
            versions.remove(old_version)
            try:
                old = self.client.get_object(Bucket=self.bucket, Key=key, VersionId=old_version)
                _body_bytes(old, self.bounds.max_probe_bytes)
            except Exception:
                pass
            else:
                raise ValueError("deleted_version_present")
            current = self.client.get_object(Bucket=self.bucket, Key=key, VersionId=new_version)
            if _version(current) != new_version or _body_bytes(current, self.bounds.max_probe_bytes) != replacement:
                raise ValueError("replacement_changed")
            evidence = hashlib.sha256((old_version + "\0" + new_version).encode()).hexdigest()
            return self._result(CapabilityKind.CLEANUP, True, evidence=f"cleanup-v1:{evidence}")
        except Exception:
            return self._result(CapabilityKind.CLEANUP, False, failure="cleanup_unsafe")
        finally:
            self._cleanup_versions(key, versions)


__all__ = [
    "CapabilityBinding",
    "CapabilityCache",
    "CapabilityKind",
    "CapabilityResult",
    "POSITIVE_TTL",
    "PROBE_ADAPTER_VERSION",
    "ProbeBounds",
    "S3CapabilityProber",
]
