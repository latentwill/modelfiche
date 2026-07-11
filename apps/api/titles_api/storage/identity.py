from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Iterable
from urllib.parse import urlsplit


_PREFIX_SEGMENT = re.compile(r"^[^\\/:?#]+$")
_ENV_PREFIX = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ADDRESSING_STYLES = frozenset({"auto", "path", "virtual"})


class SourceIdentityError(ValueError):
    """Raised when a source identity cannot be made canonical safely."""


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    endpoint: str
    bucket: str
    region: str | None
    addressing_style: str
    credential_env_prefix: str
    allowed_prefixes: tuple[str, ...]
    managed_prefix: str
    fingerprint: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "addressing_style": self.addressing_style,
            "allowed_prefixes": list(self.allowed_prefixes),
            "bucket": self.bucket,
            "credential_env_prefix": self.credential_env_prefix,
            "endpoint": self.endpoint,
            "managed_prefix": self.managed_prefix,
            "region": self.region,
        }


def normalize_source_identity(
    *,
    endpoint_url: str | None,
    bucket: str,
    region: str | None,
    addressing_style: str,
    credential_env_prefix: str,
    allowed_prefixes: Iterable[str],
    managed_prefix: str,
) -> SourceIdentity:
    """Return the immutable, versioned identity used by snapshots and locations."""
    endpoint = _normalize_endpoint(endpoint_url)
    normalized_bucket = _required_value(bucket, "bucket")
    normalized_region = _normalize_optional(region)
    normalized_style = _required_value(addressing_style, "addressing style").lower()
    if normalized_style not in _ADDRESSING_STYLES:
        raise SourceIdentityError("addressing style must be auto, path, or virtual")

    env_prefix = _required_value(credential_env_prefix, "credential environment prefix").upper()
    if not _ENV_PREFIX.fullmatch(env_prefix):
        raise SourceIdentityError("credential environment prefix must be an uppercase identifier")

    prefixes = tuple(sorted({_normalize_prefix(value, field="allowed prefix") for value in allowed_prefixes}))
    if not prefixes:
        raise SourceIdentityError("at least one allowed prefix is required")
    managed = _normalize_prefix(managed_prefix, field="managed prefix")
    if not any(_contains_prefix(prefix, managed) for prefix in prefixes):
        raise SourceIdentityError("managed prefix must be contained by an allowed prefix")

    canonical = {
        "addressing_style": normalized_style,
        "allowed_prefixes": list(prefixes),
        "bucket": normalized_bucket,
        "credential_env_prefix": env_prefix,
        "endpoint": endpoint,
        "managed_prefix": managed,
        "region": normalized_region,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return SourceIdentity(
        endpoint=endpoint,
        bucket=normalized_bucket,
        region=normalized_region,
        addressing_style=normalized_style,
        credential_env_prefix=env_prefix,
        allowed_prefixes=prefixes,
        managed_prefix=managed,
        fingerprint=f"source-v1:{sha256(encoded).hexdigest()}",
    )


def _normalize_endpoint(value: str | None) -> str:
    if value is None or value.strip().lower() == "aws-default":
        return "aws-default"
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != "https":
        raise SourceIdentityError("endpoint must be HTTPS or aws-default")
    if parsed.username or parsed.password:
        raise SourceIdentityError("endpoint must not include userinfo")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise SourceIdentityError("endpoint must not include a path, query, or fragment")
    if not parsed.hostname:
        raise SourceIdentityError("endpoint must include a host")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except ValueError as exc:
        raise SourceIdentityError("endpoint has an invalid port") from exc
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"https://{display_host}" if port in (None, 443) else f"https://{display_host}:{port}"


def _normalize_prefix(value: str, *, field: str) -> str:
    raw = _required_value(value, field).replace("\\", "/")
    segments = [segment for segment in raw.strip("/").split("/") if segment]
    if not segments:
        raise SourceIdentityError(f"{field} must not be empty")
    if any(segment in {".", ".."} or not _PREFIX_SEGMENT.fullmatch(segment) for segment in segments):
        raise SourceIdentityError(f"{field} contains an unsafe path segment")
    return "/".join(segments) + "/"


def _contains_prefix(parent: str, child: str) -> bool:
    return child.startswith(parent)


def _required_value(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SourceIdentityError(f"{field} is required")
    return normalized


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None
