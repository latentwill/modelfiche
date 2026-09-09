from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from .. import models
from ..integrations.config import S3Settings
from .repository import StorageRepository
from .s3_client import create_source_s3_client


_VERIFIED_STATES = frozenset({"available", "verified"})
_CHUNK_SIZE = 8 * 1024 * 1024
# Some S3-compatible providers reject two-part multipart uploads; a bounded
# single PUT avoids that edge while larger objects remain resumable.
_MULTIPART_THRESHOLD = 256 * 1024 * 1024
_MULTIPART_PART_SIZE = 128 * 1024 * 1024


@dataclass(slots=True)
class SingleCopyReport:
    """A redacted, resumable result for one single-copy operation."""

    examined: int = 0
    already_remote: int = 0
    uploaded: int = 0
    reused: int = 0
    evicted: int = 0
    stale_pruned: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_uploaded: int = 0
    bytes_evicted: int = 0
    would_upload: int = 0
    would_evict: int = 0
    bytes_would_upload: int = 0
    bytes_would_evict: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def add_failure(self, asset_id: str, code: str) -> None:
        self.failed += 1
        self.failures.append({"asset_id": str(asset_id), "code": code})


class SingleCopyError(RuntimeError):
    """An operation could not safely process an asset."""


def _roots(roots: Iterable[Path]) -> tuple[Path, ...]:
    result = tuple(Path(root).expanduser().resolve() for root in roots)
    if not result:
        raise ValueError("at least one local storage root is required")
    return result


def _local_path(uri: str, roots: tuple[Path, ...]) -> Path:
    raw = uri.removeprefix("file://")
    path = Path(raw).expanduser().resolve(strict=False)
    if not any(path == root or root in path.parents for root in roots):
        raise SingleCopyError("outside_root")
    return path


def _hash_file(path: Path) -> tuple[int, str]:
    digest = sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _normal_prefix(value: str) -> str:
    # Source identity normally canonicalizes this field. Keep this helper
    # deliberately strict enough that a malformed legacy row cannot escape its
    # configured prefix.
    parts = [part for part in value.replace("\\", "/").strip("/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise SingleCopyError("invalid_managed_prefix")
    return "/".join(parts) + "/"


def _source_prefix(source: models.ImportSource) -> str:
    managed = _normal_prefix(source.managed_prefix)
    allowed = tuple(_normal_prefix(value) for value in (source.allowed_prefixes or ()))
    if not allowed or not any(managed.startswith(prefix) for prefix in allowed):
        raise SingleCopyError("managed_prefix_not_allowed")
    return managed


def _extension(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    # A filename suffix is data, not a path. Reject odd values instead of
    # allowing a caller-controlled key separator or an oversized key.
    if not suffix or len(suffix) > 32 or "/" in suffix or "\\" in suffix:
        return ""
    return suffix if suffix[1:].replace("_", "").isalnum() else ""


def _object_key(source: models.ImportSource, digest: str, name: str) -> str:
    return f"{_source_prefix(source)}library/{digest[:2]}/{digest}{_extension(name)}"


def _source_settings(source: models.ImportSource) -> S3Settings:
    return S3Settings.for_source(
        endpoint_url=source.endpoint_url,
        bucket=source.bucket,
        allowed_prefixes=tuple(source.allowed_prefixes or ()),
        region=source.region,
        addressing_style=source.addressing_style,
        credential_env_prefix=source.credential_env_prefix,
    )


def _assets(db: Session, workspace_id: str) -> list[models.Asset]:
    return list(
        db.scalars(
            select(models.Asset)
            .options(selectinload(models.Asset.locations))
            .where(models.Asset.workspace_id == str(workspace_id))
            .order_by(models.Asset.id)
        )
    )


def _remote_verified(
    location: models.AssetLocation,
    asset: models.Asset,
    *,
    size: int | None = None,
    source: models.ImportSource | None = None,
) -> bool:
    if location.provider != "s3" or location.source_id is None:
        return False
    if source is None or not source.is_active or location.source_id != source.id:
        return False
    expected = asset.sha256
    if not expected or location.verification_state not in _VERIFIED_STATES:
        return False
    if location.verified_sha256 != expected:
        return False
    if size is not None and location.verified_size != size:
        return False
    if location.verified_size is None:
        return False
    return True

def _active_sources(
    db: Session,
    locations: Iterable[models.AssetLocation],
    workspace_id: str,
) -> dict[str, models.ImportSource]:
    ids = {str(location.source_id) for location in locations if location.provider == "s3" and location.source_id}
    if not ids:
        return {}
    return {
        str(source.id): source
        for source in db.scalars(
            select(models.ImportSource).where(
                models.ImportSource.id.in_(ids),
                models.ImportSource.workspace_id == str(workspace_id),
            )
        ).all()
        if source.is_active
    }


def _head_matches(head: dict[str, Any], digest: str, size: int) -> bool:
    if int(head.get("ContentLength", -1)) != size:
        return False
    metadata = head.get("Metadata") or {}
    remote_digest = metadata.get("sha256") or metadata.get("SHA256") or head.get("x-amz-meta-sha256")
    return isinstance(remote_digest, str) and remote_digest.lower() == digest.lower()


def _is_not_found(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = str(error.get("Code", "")).lower()
    return (
        code in {"404", "nosuchkey", "notfound", "no such key"}
        or isinstance(exc, (FileNotFoundError, KeyError))
    )


def _redacted_s3_code(exc: BaseException) -> str:
    # Never put SDK messages, URLs, request IDs, or credential material in the
    # report. The exception class is useful to operators and contains no secret.
    return "s3_not_found" if _is_not_found(exc) else "s3_error"

def _s3_error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = str(error.get("Code", "")).lower()
    if code:
        return code
    return "invalidpart" if "invalidpart" in type(exc).__name__.lower() else ""


def _listed_parts(client: Any, bucket: str, key: str, upload_id: str) -> dict[int, str]:
    parts: dict[int, str] = {}
    marker: int | None = None
    while True:
        request: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "UploadId": upload_id,
        }
        if marker is not None:
            request["PartNumberMarker"] = marker
        response = client.list_parts(**request)
        for part in response.get("Parts", ()):
            number = int(part["PartNumber"])
            etag = str(part.get("ETag", "")).strip('"')
            if etag:
                parts[number] = etag
        if not response.get("IsTruncated"):
            return parts
        next_marker = response.get("NextPartNumberMarker")
        if next_marker is None:
            return parts
        marker = int(next_marker)


def _multipart_upload(
    client: Any,
    *,
    bucket: str,
    key: str,
    path: Path,
    digest: str,
) -> None:
    created = client.create_multipart_upload(
        Bucket=bucket,
        Key=key,
        Metadata={"sha256": digest},
    )
    upload_id = str(created["UploadId"])
    parts: list[dict[str, Any]] = []
    try:
        with path.open("rb") as stream:
            number = 1
            while chunk := stream.read(_MULTIPART_PART_SIZE):
                try:
                    response = client.upload_part(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=number,
                        Body=chunk,
                    )
                    etag = str(response["ETag"]).strip('"')
                except BaseException:
                    # A timeout can mean the service accepted the part even
                    # though the request did not return. Reconcile before
                    # retrying so the same part is never needlessly uploaded.
                    accepted = _listed_parts(client, bucket, key, upload_id)
                    etag = accepted.get(number, "")
                    if not etag:
                        raise
                parts.append({"ETag": etag, "PartNumber": number})
                number += 1
        try:
            client.complete_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except BaseException as exc:
            # S4 can briefly report InvalidPart while its part index catches
            # up. Inspect the authoritative listing and retry once when every
            # expected part has been accepted.
            if _s3_error_code(exc) != "invalidpart":
                raise
            accepted = _listed_parts(client, bucket, key, upload_id)
            if any(int(part["PartNumber"]) not in accepted for part in parts):
                raise
            client.complete_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": [
                        {"ETag": accepted[int(part["PartNumber"])], "PartNumber": int(part["PartNumber"])}
                        for part in parts
                    ]
                },
            )
    except BaseException:
        try:
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        except BaseException:
            pass
        raise


def audit_single_copy(
    db: Session,
    workspace_id: str,
    source_id: str,
    roots: Iterable[Path],
) -> SingleCopyReport:
    """Inspect local and source-backed locations without changing any state."""
    roots_tuple = _roots(roots)
    report = SingleCopyReport()
    source = db.get(models.ImportSource, str(source_id))
    assets = _assets(db, workspace_id)
    for asset in assets:
        report.examined += 1
        if (
            source is None
            or source.workspace_id != str(workspace_id)
            or not source.is_active
            or source.provider != "s3"
        ):
            report.add_failure(asset.id, "source_inactive")
            continue
        remote = [
            location
            for location in asset.locations
            if location.source_id == str(source_id)
            and _remote_verified(location, asset, source=source)
        ]
        if remote:
            report.already_remote += 1
        local = [location for location in asset.locations if location.provider == "local"]
        if not local and not remote:
            report.skipped += 1
            continue
        for location in local:
            try:
                path = _local_path(location.uri, roots_tuple)
            except SingleCopyError as exc:
                report.add_failure(asset.id, str(exc))
                continue
            if not path.is_file():
                report.add_failure(asset.id, "missing_file")
    return report


def ensure_remote(
    db: Session,
    workspace_id: str,
    source_id: str,
    roots: Iterable[Path],
    *,
    dry_run: bool = False,
    client_factory: Callable[[S3Settings], Any] = create_source_s3_client,
) -> SingleCopyReport:
    """Publish local originals to one deterministic object per content digest."""
    roots_tuple = _roots(roots)
    report = SingleCopyReport()
    source = db.get(models.ImportSource, str(source_id))
    assets = _assets(db, workspace_id)
    if source is None or source.workspace_id != str(workspace_id) or not source.is_active or source.provider != "s3":
        for asset in assets:
            report.examined += 1
            report.add_failure(asset.id, "source_inactive")
        return report
    try:
        _source_prefix(source)
        settings = _source_settings(source)
    except (SingleCopyError, TypeError, ValueError):
        for asset in assets:
            report.examined += 1
            report.add_failure(asset.id, "managed_prefix_not_allowed")
        return report

    client: Any | None = None
    for asset in assets:
        asset_id = str(asset.id)
        report.examined += 1
        local_locations = [location for location in asset.locations if location.provider == "local"]
        remote_key = _object_key(source, asset.sha256, asset.name) if asset.sha256 else None
        remote_without_local = any(
            location.object_key == remote_key
            and _remote_verified(location, asset, source=source)
            for location in asset.locations
            if location.provider == "s3" and location.source_id == source.id
        )
        if not local_locations:
            if remote_without_local:
                report.already_remote += 1
            else:
                report.skipped += 1
            continue
        active_sources = _active_sources(db, asset.locations, str(workspace_id))
        if any(
            location.provider == "s3"
            and str(location.source_id) in active_sources
            and _remote_verified(
                location,
                asset,
                source=active_sources[str(location.source_id)],
            )
            for location in asset.locations
        ):
            report.already_remote += 1
            continue
        local_path: Path | None = None
        try:
            for candidate in local_locations:
                path = _local_path(candidate.uri, roots_tuple)
                if path.is_file():
                    local_path = path
                    break
            if local_path is None:
                raise SingleCopyError("missing_file")
            size, digest = _hash_file(local_path)
            if asset.sha256 and asset.sha256.lower() != digest.lower():
                raise SingleCopyError("checksum_mismatch")
            key = _object_key(source, digest, asset.name)
            existing = next(
                (
                    location
                    for location in asset.locations
                    if location.provider == "s3"
                    and location.source_id == source.id
                    and location.object_key == key
                    and _remote_verified(location, asset, size=size, source=source)
                ),
                None,
            )
            if existing is not None:
                report.already_remote += 1
                report.reused += 1
                continue
            if dry_run:
                # No client construction, head, database flush, or filesystem
                # mutation occurs in dry-run mode.
                report.would_upload += 1
                report.bytes_would_upload += size
                continue
            if client is None:
                client = client_factory(settings)
            reused = False
            try:
                head = client.head_object(Bucket=source.bucket, Key=key)
                reused = _head_matches(head, digest, size)
                if not reused:
                    raise SingleCopyError("remote_checksum_mismatch")
            except SingleCopyError:
                raise
            except BaseException as exc:
                if not _is_not_found(exc):
                    # An unknown remote state is not safe to turn into an
                    # upload: never overwrite an object we could not inspect.
                    raise SingleCopyError(_redacted_s3_code(exc)) from exc
            if not reused:
                if size > _MULTIPART_THRESHOLD:
                    _multipart_upload(
                        client,
                        bucket=source.bucket,
                        key=key,
                        path=local_path,
                        digest=digest,
                    )
                else:
                    with local_path.open("rb") as body:
                        client.put_object(
                            Bucket=source.bucket,
                            Key=key,
                            Body=body,
                            Metadata={"sha256": digest},
                        )
                report.uploaded += 1
                report.bytes_uploaded += size
                head = client.head_object(Bucket=source.bucket, Key=key)
                if not _head_matches(head, digest, size):
                    raise SingleCopyError("remote_verification_failed")
            etag = str(head.get("ETag", "")).strip('"') or None
            with db.begin_nested():
                StorageRepository(db, str(workspace_id)).attach_verified_remote_location(
                    asset_id=asset_id,
                    source_id=source.id,
                    object_key=key,
                    etag=etag,
                    size=size,
                    sha256=digest,
                    mime_type=asset.mime_type or "application/octet-stream",
                    modified_at=head.get("LastModified"),
                )
                db.flush()
            if reused:
                report.reused += 1
        except SingleCopyError as exc:
            report.add_failure(asset_id, str(exc))
        except (OSError, ValueError) as exc:
            report.add_failure(asset_id, "local_error" if isinstance(exc, OSError) else "registration_error")
        except BaseException as exc:
            report.add_failure(asset_id, _redacted_s3_code(exc))
    return report



def _retire_local_location(location: models.AssetLocation) -> None:
    # Keep the historical row because durable migration/checkpoint records may
    # reference it. Eligibility is state-gated, so it cannot be selected after
    # the physical copy is gone.
    location.verification_state = "missing"
    location.hydration_state = "remote_missing"



def evict_local(
    db: Session,
    workspace_id: str,
    roots: Iterable[Path],
    *,
    dry_run: bool = False,
) -> SingleCopyReport:
    """Remove local copies only when an active source has an exact verified copy."""
    roots_tuple = _roots(roots)
    report = SingleCopyReport()
    assets = _assets(db, workspace_id)
    for asset in assets:
        report.examined += 1
        local_locations = [
            location
            for location in asset.locations
            if location.provider == "local"
            and not (
                location.verification_state == "missing"
                and location.hydration_state == "remote_missing"
            )
        ]
        if not local_locations:
            report.skipped += 1
            continue
        active = _active_sources(db, asset.locations, str(workspace_id))
        remote = next(
            (
                location
                for location in asset.locations
                if location.provider == "s3"
                and str(location.source_id) in active
                and _remote_verified(location, asset, source=active[str(location.source_id)])
            ),
            None,
        )
        for location in local_locations:
            try:
                path = _local_path(location.uri, roots_tuple)
                if not path.is_file():
                    if not dry_run:
                        with db.begin_nested():
                            if remote is not None:
                                if asset.preferred_location_id == location.id:
                                    asset.preferred_location_id = remote.id
                                if asset.origin_location_id == location.id:
                                    asset.origin_location_id = remote.id
                            else:
                                if asset.preferred_location_id == location.id:
                                    asset.preferred_location_id = None
                                if asset.origin_location_id == location.id:
                                    asset.origin_location_id = None
                            _retire_local_location(location)
                            db.flush()
                    report.stale_pruned += 1
                    continue
                if remote is None:
                    report.skipped += 1
                    continue
                size = path.stat().st_size
                if not _remote_verified(remote, asset, size=size, source=active[str(remote.source_id)]):
                    report.add_failure(asset.id, "remote_verification_failed")
                    continue
                if dry_run:
                    report.would_evict += 1
                    report.bytes_would_evict += size
                    continue
                with db.begin_nested():
                    if asset.preferred_location_id == location.id:
                        asset.preferred_location_id = remote.id
                    if asset.origin_location_id == location.id:
                        asset.origin_location_id = remote.id
                    _retire_local_location(location)
                    db.flush()
                    path.unlink()
                report.evicted += 1
                report.bytes_evicted += size
            except SingleCopyError as exc:
                report.add_failure(asset.id, str(exc))
            except OSError:
                report.add_failure(asset.id, "unlink_failed")
            except SQLAlchemyError:
                report.add_failure(asset.id, "database_reference")
    return report


__all__ = ["SingleCopyReport", "audit_single_copy", "ensure_remote", "evict_local"]
