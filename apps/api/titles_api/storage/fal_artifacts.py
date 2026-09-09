from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_ARTIFACT_URL_BYTES = 2048


def validate_artifact_url(url: str) -> str:
    """Validate a fresh in-memory provider URL without persisting it."""
    if not isinstance(url, str) or not url or len(url.encode("utf-8")) > MAX_ARTIFACT_URL_BYTES:
        raise ArtifactShapeError("provider artifact URL is invalid")
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ArtifactShapeError("provider artifact URL must be a public HTTPS URL")
    return url


def bounded_sha256(chunks: Sequence[bytes], *, max_bytes: int = MAX_ARTIFACT_BYTES) -> tuple[int, str]:
    """Hash bounded artifact bytes; callers fetch chunks through SafeHttpTransport."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    digest = sha256()
    size = 0
    for chunk in chunks:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ArtifactShapeError("artifact body must contain byte chunks")
        raw = bytes(chunk)
        size += len(raw)
        if size > max_bytes:
            raise ArtifactShapeError("artifact exceeds the configured byte limit")
        digest.update(raw)
    return size, digest.hexdigest()


class ArtifactShapeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CanonicalArtifact:
    ordinal: int
    identity: dict[str, Any]
    metadata: dict[str, Any]
    download_url: str


@dataclass(frozen=True, slots=True)
class CanonicalArtifactList:
    digest: str
    items: tuple[CanonicalArtifact, ...]


def canonicalize_artifacts(
    artifacts: Sequence[Mapping[str, Any]],
    *,
    expected_ordinals: Sequence[int],
) -> CanonicalArtifactList:
    expected = tuple(expected_ordinals)
    if len(expected) != len(set(expected)):
        raise ArtifactShapeError("expected ordinals must be unique")

    by_ordinal: dict[int, CanonicalArtifact] = {}
    for artifact in artifacts:
        item = _canonical_artifact(artifact)
        if item.ordinal in by_ordinal:
            raise ArtifactShapeError("provider artifact ordinals must be unique")
        by_ordinal[item.ordinal] = item

    if set(by_ordinal) != set(expected):
        raise ArtifactShapeError("provider artifact ordinals do not match expected ordinals")

    items = tuple(by_ordinal[ordinal] for ordinal in sorted(expected))
    payload = [
        {"ordinal": item.ordinal, "identity": item.identity, "metadata": item.metadata}
        for item in items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return CanonicalArtifactList(digest=sha256(encoded).hexdigest(), items=items)

def _canonical_artifact(value: Mapping[str, Any]) -> CanonicalArtifact:
    ordinal = value.get("ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise ArtifactShapeError("provider artifact ordinal must be a non-negative integer")

    download_url = validate_artifact_url(value.get("url"))

    metadata = _metadata(value)
    artifact_id = value.get("artifact_id")
    if artifact_id is not None and (not isinstance(artifact_id, str) or not artifact_id):
        raise ArtifactShapeError("provider artifact_id must be a non-empty string")
    identity = {"provider_artifact_id": artifact_id} if artifact_id else {"ordinal": ordinal, **metadata}
    return CanonicalArtifact(ordinal=ordinal, identity=identity, metadata=metadata, download_url=download_url)


def _metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    mime = value.get("mime")
    width = value.get("width")
    height = value.get("height")
    seed = value.get("seed")
    if not isinstance(mime, str) or not mime:
        raise ArtifactShapeError("provider artifact MIME is required")
    if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
        raise ArtifactShapeError("provider artifact width must be a positive integer")
    if not isinstance(height, int) or isinstance(height, bool) or height <= 0:
        raise ArtifactShapeError("provider artifact height must be a positive integer")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise ArtifactShapeError("provider artifact seed must be an integer or null")
    return {"mime": mime, "width": width, "height": height, "seed": seed}
