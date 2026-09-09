from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import tempfile
from threading import BoundedSemaphore, Lock
from urllib.parse import unquote, urlparse
from uuid import UUID
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..integrations.config import S3Settings
from ..services import current_workspace
from ..settings import get_settings
from ..storage.cache import ThumbnailCache
from ..storage.capabilities import CapabilityKind
from ..storage.contracts import (
    AlternateDeliveryRequestV1,
    AssetDeliveryDescriptorV1,
    AssetDeliveryRequestV1,
    AssetDeliveryResultV1,
    ContentVariantV1,
    DeliveryFailureV1,
    DiagnoseDeliveryRequestV1,
    DiagnoseDeliveryResultV1,
    FailureDeliveryResultV1,
    ThumbnailVariantV1,
)
from ..storage.delivery import DeliveryCapability, DeliveryTokenError, DeliveryTokenLedger
from ..storage.repository import StorageNotFound, StorageRepository
from ..storage.thumbnails import ThumbnailError, ThumbnailRenderer
from ..storage.gates import GateState, StorageGateStore
from ..storage.s3_client import cached_source_s3_client


_thumbnail_components: tuple[ThumbnailCache, ThumbnailRenderer] | None = None
_thumbnail_components_lock = Lock()


def _shared_thumbnail_components() -> tuple[ThumbnailCache, ThumbnailRenderer]:
    global _thumbnail_components
    if _thumbnail_components is None:
        with _thumbnail_components_lock:
            if _thumbnail_components is None:
                settings = get_settings()
                cache = ThumbnailCache(settings.cache_root.resolve(), quota_bytes=settings.cache_root_quota_bytes)
                _thumbnail_components = (cache, ThumbnailRenderer(cache))
    return _thumbnail_components


router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


@dataclass(frozen=True, slots=True)
class DeliveryLimits:
    """Safety limits for one process's delivery capabilities and proxy streams."""

    capability_ttl: timedelta = timedelta(minutes=5)
    alternate_ttl: timedelta = timedelta(seconds=60)
    max_proxy_bytes: int = 512 * 1024 * 1024
    global_slots: int = 8
    per_source_slots: int = 2

    def __post_init__(self) -> None:
        if self.capability_ttl.total_seconds() <= 0 or self.alternate_ttl.total_seconds() <= 0:
            raise ValueError("delivery capability lifetimes must be positive")
        if self.max_proxy_bytes <= 0 or self.global_slots <= 0 or self.per_source_slots <= 0:
            raise ValueError("delivery limits must be positive")


class DeliveryCapacityError(RuntimeError):
    """The bounded proxy cannot accept another stream."""


class DeliverySourceError(RuntimeError):
    """A source cannot be read without exposing unsafe or ambiguous bytes."""


class DeliveryConfigurationError(DeliverySourceError):
    """Remote delivery is disabled or its immutable source is not ready."""


class _ReleaseOnce:
    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback: Callable[[], None] | None = callback
        self._lock = Lock()

    def __call__(self) -> None:
        with self._lock:
            callback, self._callback = self._callback, None
        if callback is not None:
            callback()


@dataclass(frozen=True, slots=True)
class LocalPayload:
    path: Path
    size: int | None
    mime_type: str
    source_id: str = "local"
    release: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class S3Payload:
    client: object
    bucket: str
    key: str
    source_id: str
    mime_type: str
    size: int | None
    etag: str | None
    version_id: str | None
    expected_sha256: str | None
    mode: str


@dataclass(frozen=True, slots=True)
class _Lease:
    global_slots: BoundedSemaphore
    source_slots: BoundedSemaphore

    def release(self) -> None:
        self.source_slots.release()
        self.global_slots.release()


class _DeliverySlots:
    def __init__(self, limits: DeliveryLimits) -> None:
        self._global = BoundedSemaphore(limits.global_slots)
        self._limits = limits
        self._sources: dict[str, BoundedSemaphore] = {}
        self._lock = Lock()

    def acquire(self, source_id: str) -> _Lease:
        if not self._global.acquire(blocking=False):
            raise DeliveryCapacityError("delivery capacity is exhausted")
        with self._lock:
            source = self._sources.setdefault(source_id, BoundedSemaphore(self._limits.per_source_slots))
        if not source.acquire(blocking=False):
            self._global.release()
            raise DeliveryCapacityError("delivery source capacity is exhausted")
        return _Lease(self._global, source)


@dataclass(frozen=True, slots=True)
class _Selected:
    asset: models.Asset
    location: models.AssetLocation
    alternate: models.AssetLocation | None


class AssetDeliveryService:
    """Resolve immutable revisions and issue process-local delivery capabilities."""
    _s3_buffer_max_bytes = 64 * 1024 * 1024

    def __init__(
        self,
        *,
        ledger: DeliveryTokenLedger | None = None,
        limits: DeliveryLimits | None = None,
        client_factory: Callable[[models.ImportSource], object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.limits = limits or DeliveryLimits()
        self.ledger = ledger or DeliveryTokenLedger()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client_factory = client_factory or self._client
        self._slots = _DeliverySlots(self.limits)

    def descriptor(
        self,
        db: Session,
        *,
        asset_revision_id: UUID,
        variant: ContentVariantV1 | ThumbnailVariantV1,
        host: str,
        base_url: str = "",
        forced_location_id: UUID | None = None,
        fallback_used: bool = False,
    ) -> AssetDeliveryResultV1:
        try:
            selected = self._select(db, asset_revision_id, forced_location_id)
        except (StorageNotFound, ValueError):
            return _failure("native_delivery_failed", "The requested image revision is unavailable.", False, "stop")
        if selected.asset.kind is not models.AssetKind.image:
            return _failure("native_delivery_failed", "Only image revisions can be delivered.", False, "stop")
        try:
            payload, mode = self._payload(db, selected, variant)
        except DeliveryCapacityError:
            return _failure("delivery_capacity", "Image delivery capacity is temporarily exhausted.", True, "user_retry_once")
        except DeliveryConfigurationError:
            if selected.location.provider == "s3" and selected.alternate is not None and selected.alternate.provider == "local":
                selected = _Selected(selected.asset, selected.alternate, None)
                fallback_used = True
                try:
                    payload, mode = self._payload(db, selected, variant)
                except DeliverySourceError:
                    return _failure("native_delivery_failed", "The local image copy is unavailable.", True, "user_retry_once")
                except DeliveryConfigurationError:
                    return _failure("storage_configuration_required", "Image storage needs attention.", False, "open_storage_details", asset_revision_id=asset_revision_id)
            else:
                return _failure("storage_configuration_required", "Image storage needs attention.", False, "open_storage_details", asset_revision_id=asset_revision_id)
        except DeliverySourceError:
            if selected.alternate is not None and not fallback_used:
                alternate = self.ledger.issue_alternate(
                    asset_id=UUID(str(selected.asset.id)),
                    asset_revision_id=asset_revision_id,
                    alternate_location_id=UUID(str(selected.alternate.id)),
                    variant=variant,
                    host=host,
                    expires_at=self._now() + self.limits.alternate_ttl,
                )
                return _failure(
                    "primary_failed_alternate_available",
                    "The preferred image copy is unavailable.",
                    True,
                    "consume_alternate_once",
                    alternate_token=alternate.token,
                )
            return _failure("native_delivery_failed", "The image could not be loaded.", True, "user_retry_once")
        delivery_url: str | None = None
        if mode == "direct_s3":
            try:
                delivery_url = str(payload.client.generate_presigned_url("get_object", Params={"Bucket": payload.bucket, "Key": payload.key, "VersionId": payload.version_id}, ExpiresIn=int(self.limits.capability_ttl.total_seconds())))
            except Exception:
                return _failure("storage_configuration_required", "Image storage needs attention.", False, "open_storage_details", asset_revision_id=asset_revision_id)
        expires_at = self._now() + self.limits.capability_ttl
        capability = self.ledger.issue_capability(
            asset_id=UUID(str(selected.asset.id)),
            asset_revision_id=asset_revision_id,
            location_id=UUID(str(selected.location.id)),
            variant=variant,
            host=host,
            expires_at=expires_at,
            payload=payload,
        )
        diagnostic = self.ledger.issue_diagnostic(
            asset_id=UUID(str(selected.asset.id)),
            asset_revision_id=asset_revision_id,
            location_id=UUID(str(selected.location.id)),
            variant=variant,
            host=host,
            expires_at=expires_at,
            payload=payload,
        )
        alternate_token: str | None = None
        if selected.alternate is not None:
            alternate_token = self.ledger.issue_alternate(
                asset_id=UUID(str(selected.asset.id)),
                asset_revision_id=asset_revision_id,
                alternate_location_id=UUID(str(selected.alternate.id)),
                variant=variant,
                host=host,
                expires_at=self._now() + self.limits.alternate_ttl,
            ).token
        if delivery_url is None:
            delivery_url = f"{base_url}/api/asset-deliveries/{capability.token}"
        descriptor = AssetDeliveryDescriptorV1(
            diagnostic_token=diagnostic.token,
            asset_revision_id=asset_revision_id,
            selected_location_id=UUID(str(selected.location.id)),
            provider=str(selected.location.provider),
            source_label=_source_label(selected.location, db),
            variant=variant,
            delivered_mime_type=payload.mime_type or selected.asset.mime_type or "application/octet-stream",
            delivered_size_bytes=payload.size,
            mode=mode,
            delivery_url=delivery_url,
            expires_at=expires_at,
            fallback_used=fallback_used,
            alternate_token=alternate_token,
            summary_id=f"asset:{asset_revision_id}",
        )
        return {"kind": "descriptor", "descriptor": descriptor}

    def alternate(
        self,
        db: Session,
        request: AlternateDeliveryRequestV1,
        *,
        host: str,
        base_url: str = "",
    ) -> AssetDeliveryResultV1:
        try:
            alternate = self.ledger.consume_alternate(
                request.alternate_token,
                asset_id=request.asset_revision_id,
                asset_revision_id=request.asset_revision_id,
                variant=request.variant,
                host=host,
            )
        except DeliveryTokenError as exc:
            return _failure("alternate_failed", "The alternate image copy is no longer available.", False, "stop")
        return self.descriptor(
            db,
            asset_revision_id=request.asset_revision_id,
            variant=request.variant,
            host=host,
            base_url=base_url,
            forced_location_id=alternate.alternate_location_id,
            fallback_used=True,
        )

    def diagnose(
        self,
        db: Session,
        request: DiagnoseDeliveryRequestV1,
        *,
        host: str,
    ) -> DiagnoseDeliveryResultV1:
        try:
            capability = self.ledger.resolve_diagnostic(request.diagnostic_token, host=host)
        except DeliveryTokenError:
            return {"kind": "failure", "failure": _failure_model("capability_expired", "The image delivery capability expired.", True, "reacquire_descriptor_once")}
        payload = capability.payload
        try:
            if isinstance(payload, S3Payload) and payload.mode == "versioned":
                payload.client.head_object(Bucket=payload.bucket, Key=payload.key, VersionId=payload.version_id)
            elif isinstance(payload, S3Payload) and payload.mode == "conditional":
                payload.client.head_object(Bucket=payload.bucket, Key=payload.key, IfMatch=payload.etag)
        except Exception:
            return {"kind": "failure", "failure": _failure_model("native_delivery_failed", "The image could not be loaded.", True, "user_retry_once")}
        return {"kind": "failure", "failure": _failure_model("native_delivery_failed", "The image could not be loaded.", True, "user_retry_once")}

    def serve(self, token: str, request: Request, *, host: str) -> StreamingResponse | Response | JSONResponse:
        try:
            capability = self.ledger.resolve_capability(token, host=host)
        except DeliveryTokenError:
            return _failure_response(_failure_model("capability_expired", "The image delivery capability expired.", True, "reacquire_descriptor_once"), status.HTTP_410_GONE)
        try:
            lease = self._slots.acquire(str(capability.location_id))
        except DeliveryCapacityError:
            return _failure_response(_failure_model("delivery_capacity", "Image delivery capacity is temporarily exhausted.", True, "user_retry_once"), status.HTTP_429_TOO_MANY_REQUESTS)
        headers: dict[str, str] = {"Cache-Control": "private, max-age=31536000, immutable"}
        expected_sha256 = getattr(capability.payload, "expected_sha256", None)
        if expected_sha256:
            headers["ETag"] = f'"{expected_sha256}"'
        # If-None-Match takes precedence over Range per RFC 7233.
        if headers.get("ETag") and request.headers.get("if-none-match") == headers["ETag"]:
            lease.release()
            self._release_payload(capability.payload)
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
        try:
            total = self._preflight(capability.payload)
        except DeliverySourceError:
            lease.release()
            self._release_payload(capability.payload)
            return _failure_response(_failure_model("native_delivery_failed", "The image could not be loaded.", True, "user_retry_once"), status.HTTP_503_SERVICE_UNAVAILABLE)
        payload_size = getattr(capability.payload, "size", None)
        total = total if total is not None else payload_size
        if total is not None:
            headers["Content-Length"] = str(total)
        byte_range = self._parse_range(request.headers.get("range"), total)
        if byte_range == "unsatisfiable":
            lease.release()
            self._release_payload(capability.payload)
            return Response(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                headers={**headers, "Content-Range": f"bytes */{total}"},
            )
        try:
            iterator = self._stream(capability.payload, lease, byte_range=byte_range or None)
            # Priming the iterator runs source validation and buffered reads
            # before any response bytes are committed, so failures return a
            # clean 503 instead of a truncated 200.
            first = next(iterator)
        except StopIteration:
            first = b""
        except DeliverySourceError:
            lease.release()
            self._release_payload(capability.payload)
            return _failure_response(_failure_model("native_delivery_failed", "The image could not be loaded.", True, "user_retry_once"), status.HTTP_503_SERVICE_UNAVAILABLE)
        media = capability.variant.kind == "thumbnail" and "image/webp" or getattr(capability.payload, "mime_type", "application/octet-stream")
        if isinstance(byte_range, tuple):
            start, end = byte_range
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            headers["Content-Length"] = str(end - start + 1)
            return StreamingResponse(self._release_when_done(first, iterator, lease, capability.payload), status_code=status.HTTP_206_PARTIAL_CONTENT, media_type=media, headers=headers)
        return StreamingResponse(self._release_when_done(first, iterator, lease, capability.payload), media_type=media, headers=headers)

    @staticmethod
    def _parse_range(header: str | None, total: int | None) -> tuple[int, int] | str | None:
        """Return (start, end), "unsatisfiable", or None to ignore the range."""
        if not header or total is None or total <= 0:
            return None
        if "," in header:
            return None
        parts = header.split("=", 1)
        if len(parts) != 2 or parts[0].strip().lower() != "bytes":
            return None
        start_text, _, end_text = parts[1].strip().partition("-")
        try:
            if start_text == "":
                suffix = int(end_text)
                if suffix <= 0:
                    return "unsatisfiable"
                start = max(total - suffix, 0)
                end = total - 1
            else:
                start = int(start_text)
                end = int(end_text) if end_text else total - 1
        except ValueError:
            return None
        if end >= total:
            end = total - 1
        if start > end or start >= total:
            return "unsatisfiable"
        return start, end

    @staticmethod
    def _release_payload(payload: object) -> None:
        release = getattr(payload, "release", None)
        if release is not None:
            release()

    @classmethod
    def _release_when_done(cls, first: bytes, iterator: Iterator[bytes], lease, payload: object) -> Iterator[bytes]:
        try:
            yield first
            yield from iterator
        finally:
            lease.release()
            cls._release_payload(payload)

    @staticmethod
    def _preflight(payload: object) -> int | None:
        """Validate the remote source and return its authoritative length."""
        if not isinstance(payload, S3Payload):
            return getattr(payload, "size", None)
        kwargs = {"Bucket": payload.bucket, "Key": payload.key}
        if payload.etag:
            kwargs["IfMatch"] = payload.etag
        if payload.version_id:
            kwargs["VersionId"] = payload.version_id
        try:
            head = payload.client.head_object(**kwargs)
        except Exception as exc:
            raise DeliverySourceError("source validation failed") from exc
        if payload.size is not None and head.get("ContentLength") is not None and int(head["ContentLength"]) != payload.size:
            raise DeliverySourceError("source size drifted")
        return int(head["ContentLength"]) if head.get("ContentLength") is not None else payload.size

    def _stream(self, payload: object, lease: _Lease, byte_range: tuple[int, int] | None = None) -> Iterator[bytes]:
        try:
            if isinstance(payload, LocalPayload):
                if not payload.path.is_file():
                    raise DeliverySourceError("local content is missing")
                if payload.size is not None and payload.size > self.limits.max_proxy_bytes:
                    raise DeliverySourceError("proxy byte limit exceeded")
                with payload.path.open("rb") as stream:
                    if byte_range is not None:
                        start, end = byte_range
                        stream.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = stream.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            yield chunk
                    else:
                        yield from _bounded_chunks(stream, self.limits.max_proxy_bytes)
                return
            if not isinstance(payload, S3Payload):
                raise DeliverySourceError("unknown delivery payload")
            if payload.mode == "conditional":
                try:
                    head = payload.client.head_object(Bucket=payload.bucket, Key=payload.key, IfMatch=payload.etag)
                except Exception as exc:
                    raise DeliverySourceError("conditional source validation failed") from exc
                if payload.size is not None and head.get("ContentLength") is not None and int(head["ContentLength"]) != payload.size:
                    raise DeliverySourceError("conditional source size drifted")
                actual_etag = str(head.get("ETag") or "").strip('"')
                if payload.etag and actual_etag and actual_etag != payload.etag.strip('"'):
                    raise DeliverySourceError("conditional source ETag drifted")
            kwargs = {"Bucket": payload.bucket, "Key": payload.key}
            if payload.etag:
                kwargs["IfMatch"] = payload.etag
            if payload.version_id:
                kwargs["VersionId"] = payload.version_id
            if byte_range is not None:
                start, end = byte_range
                kwargs["Range"] = f"bytes={start}-{end}"
            response = payload.client.get_object(**kwargs)
            body = response.get("Body") if isinstance(response, dict) else None
            if body is None or not hasattr(body, "read"):
                raise DeliverySourceError("S3 response has no body")
            # Buffering whole objects up to the bound lets validation complete
            # before the first response byte; ranged or oversized reads stream.
            buffered = byte_range is None and (
                payload.mode == "spool"
                or (payload.size is not None and payload.size <= self._s3_buffer_max_bytes)
            )
            if buffered:
                with tempfile.TemporaryFile() as temp:
                    digest = hashlib.sha256()
                    total = 0
                    for chunk in _bounded_chunks(body, self.limits.max_proxy_bytes):
                        total += len(chunk)
                        digest.update(chunk)
                        temp.write(chunk)
                    if payload.size is not None and total != payload.size:
                        raise DeliverySourceError("S3 content size drifted")
                    if payload.expected_sha256 and digest.hexdigest() != payload.expected_sha256.lower():
                        raise DeliverySourceError("S3 content checksum drifted")
                    temp.seek(0)
                    yield from _bounded_chunks(temp, self.limits.max_proxy_bytes)
            else:
                yield from _bounded_chunks(body, self.limits.max_proxy_bytes)
        except DeliverySourceError:
            raise
        except Exception as exc:
            raise DeliverySourceError("source read failed") from exc

    def _select(self, db: Session, asset_revision_id: UUID, forced_location_id: UUID | None) -> _Selected:
        workspace = current_workspace(db)
        repository = StorageRepository(db, workspace.id)
        asset = repository.get_asset_revision(str(asset_revision_id))
        if forced_location_id is not None:
            location = db.scalar(select(models.AssetLocation).where(models.AssetLocation.id == str(forced_location_id), models.AssetLocation.asset_id == asset.id, models.AssetLocation.workspace_id == workspace.id))
            if location is None:
                raise StorageNotFound("asset location")
            alternate = None
        else:
            location, alternate = repository.select_delivery_locations(asset.id)
            if location is None:
                raise StorageNotFound("delivery location")
        return _Selected(asset, location, alternate)

    def _payload(self, db: Session, selected: _Selected, variant: ContentVariantV1 | ThumbnailVariantV1) -> tuple[object, str]:
        location = selected.location
        if location.provider == "local":
            path = _safe_local_path(location)
            payload: object = LocalPayload(path, location.size or path.stat().st_size, selected.asset.mime_type or "application/octet-stream")
            if variant.kind == "thumbnail":
                payload = self._thumbnail(payload, selected, variant)
            return payload, "proxy"
        if location.provider != "s3":
            raise DeliverySourceError("unsupported delivery provider")
        if not self._remote_ready(db, location):
            raise DeliveryConfigurationError("remote delivery is disabled")
        source = db.get(models.ImportSource, location.source_id) if location.source_id else None
        if source is None or not source.is_active:
            raise DeliveryConfigurationError("S3 source is unavailable")
        try:
            client = self._client_factory(source)
        except Exception as exc:
            raise DeliveryConfigurationError("S3 credentials are unavailable") from exc
        bucket = location.bucket or source.bucket
        key = location.object_key or _key_from_uri(location.uri)
        if not bucket or not key:
            raise DeliveryConfigurationError("S3 location is incomplete")
        versioned = self._capability(db, source.id, CapabilityKind.VERSIONED_READ.value) and bool(location.version_id)
        conditional = self._capability(db, source.id, CapabilityKind.CONDITIONAL_READ.value) and bool(location.etag)
        mode = "versioned" if versioned else "conditional" if conditional else "spool"
        payload = S3Payload(client, bucket, key, str(source.id), selected.asset.mime_type or "application/octet-stream", location.size, location.etag, location.version_id, location.verified_sha256 or selected.asset.sha256, mode)
        if variant.kind == "thumbnail":
            payload = self._thumbnail(payload, selected, variant)
            return payload, "proxy"
        if versioned:
            return payload, "direct_s3"
        return payload, "proxy"

    def _thumbnail(self, payload: LocalPayload | S3Payload, selected: _Selected, variant: ThumbnailVariantV1) -> LocalPayload:
        cache, renderer = _shared_thumbnail_components()
        cache_key = renderer.cache_key(UUID(str(selected.asset.id)), variant.max_pixels)
        try:
            cached = cache.acquire(cache_key)
        except KeyError:
            cached = None
        else:
            return LocalPayload(
                cached,
                cached.stat().st_size,
                "image/webp",
                payload.source_id,
                _ReleaseOnce(lambda: cache.release(cache_key)),
            )
        if isinstance(payload, LocalPayload):
            try:
                source = payload.path.open("rb")
            except OSError as exc:
                raise DeliverySourceError("thumbnail source is unavailable") from exc
            source_size = payload.size
        else:
            kwargs = {"Bucket": payload.bucket, "Key": payload.key}
            if payload.etag:
                kwargs["IfMatch"] = payload.etag
            if payload.version_id:
                kwargs["VersionId"] = payload.version_id
            try:
                response = payload.client.get_object(**kwargs)
                source = response["Body"]
                content_length = response.get("ContentLength")
                if payload.size is not None and content_length is not None and int(content_length) != payload.size:
                    source.close()
                    raise DeliverySourceError("thumbnail source size drifted")
                source_size = payload.size or content_length
            except DeliverySourceError:
                raise
            except Exception as exc:
                raise DeliverySourceError("thumbnail source is unavailable") from exc
        try:
            result = renderer.render(
                asset_revision_id=UUID(str(selected.asset.id)),
                max_pixels=variant.max_pixels,
                source=source,
                source_size=source_size,
                expected_sha256=payload.expected_sha256 if isinstance(payload, S3Payload) else selected.asset.sha256,
            )
            return LocalPayload(
                result.path,
                result.path.stat().st_size,
                "image/webp",
                payload.source_id,
                _ReleaseOnce(lambda: renderer.release(result)),
            )
        except ThumbnailError as exc:
            if "capacity" in str(exc).lower():
                raise DeliveryCapacityError(str(exc)) from exc
            raise DeliverySourceError(str(exc)) from exc
        finally:
            close = getattr(source, "close", None)
            if close:
                close()

    @staticmethod
    def _capability(db: Session, source_id: str, capability: str) -> bool:
        row = db.scalar(select(models.SourceCapability).where(models.SourceCapability.source_id == source_id, models.SourceCapability.capability == capability, models.SourceCapability.state.in_(("available", "supported", "ready")), models.SourceCapability.expires_at > models.utcnow()).order_by(models.SourceCapability.expires_at.desc()))
        return row is not None

    @staticmethod
    def _remote_ready(db: Session, location: models.AssetLocation) -> bool:
        try:
            connection = db.connection()
            raw = getattr(connection.connection, "driver_connection", connection.connection)
            projection = StorageGateStore(raw).get("remote_delivery")
            if projection.state is GateState.ready:
                return True
        except Exception:
            pass
        if location.verification_state not in {"available", "verified"} or not location.verified_sha256:
            return False
        source = db.get(models.ImportSource, location.source_id) if location.source_id else None
        return source is not None and source.is_active

    @staticmethod
    def _client(source: models.ImportSource) -> object:
        settings = S3Settings.for_source(endpoint_url=source.endpoint_url, bucket=source.bucket, allowed_prefixes=tuple(source.allowed_prefixes or ()), region=source.region, addressing_style=source.addressing_style, credential_env_prefix=source.credential_env_prefix)
        return cached_source_s3_client(settings)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("delivery clock must return an aware datetime")
        return value


def _bounded_chunks(stream: object, maximum: int, *, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    total = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return
        if not isinstance(chunk, (bytes, bytearray)):
            raise DeliverySourceError("source returned a non-byte chunk")
        total += len(chunk)
        if total > maximum:
            raise DeliverySourceError("proxy byte limit exceeded")
        yield bytes(chunk)


def _safe_local_path(location: models.AssetLocation) -> Path:
    path = Path(location.uri.removeprefix("file://")).expanduser().resolve()
    settings = get_settings()
    roots = (settings.asset_root.resolve(), settings.cache_root.resolve())
    if any(_is_within_storage_root(path, root) for root in roots):
        if path.is_file():
            return path
        raise DeliverySourceError("local content is missing")
    rebased = _rebase_moved_storage_path(path, roots)
    if rebased is not None:
        return rebased
    raise DeliverySourceError("local location is outside configured storage roots")


def _rebase_moved_storage_path(path: Path, roots: tuple[Path, ...]) -> Path | None:
    """Resolve content after the application directory moves with its var tree."""
    path_parts = path.parts
    for root in roots:
        marker = root.parts[-2:]
        if not marker:
            continue
        for index in range(len(path_parts) - len(marker) + 1):
            if path_parts[index:index + len(marker)] != marker:
                continue
            candidate = root.joinpath(*path_parts[index + len(marker):]).resolve()
            if _is_within_storage_root(candidate, root) and candidate.is_file():
                return candidate
    return None


def _is_within_storage_root(path: Path, root: Path) -> bool:
    try:
        if os.path.commonpath((str(path), str(root))) == str(root):
            return True
        # macOS commonly presents the same case-insensitive path with different
        # casing after a process restart; only apply this fallback to an existing
        # path, after resolving symlinks above.
        return path.exists() and os.path.commonpath((str(path).casefold(), str(root).casefold())) == str(root).casefold()
    except ValueError:
        return False


def _key_from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        return unquote(parsed.path.lstrip("/"))
    return unquote(uri.split("?", 1)[0].split("#", 1)[0].lstrip("/"))


def _source_label(location: models.AssetLocation, db: Session) -> str:
    if location.provider == "local":
        return "This computer"
    source = db.get(models.ImportSource, location.source_id) if location.source_id else None
    return source.name if source is not None else "S3 storage"


def _failure(code: str, message: str, retryable: bool, action: str, *, alternate_token: str | None = None, asset_revision_id: UUID | None = None) -> FailureDeliveryResultV1:
    return {"kind": "failure", "failure": _failure_model(code, message, retryable, action, alternate_token=alternate_token, asset_revision_id=asset_revision_id)}


def _failure_model(code: str, message: str, retryable: bool, action: str, *, alternate_token: str | None = None, asset_revision_id: UUID | None = None) -> DeliveryFailureV1:
    if action == "consume_alternate_once":
        next_action = {"kind": action, "alternate_token": alternate_token or "unavailable"}
    elif action == "open_storage_details":
        next_action = {"kind": action, "href": f"#/image/{asset_revision_id or UUID(int=0)}", "focus_target_id": "storage_details"}
    else:
        next_action = {"kind": action}
    return DeliveryFailureV1(code=code, redacted_message=message, retryable=retryable, action=next_action)


def _failure_response(failure: DeliveryFailureV1, code: int) -> JSONResponse:
    return JSONResponse(status_code=code, content={"kind": "failure", "failure": failure.model_dump(mode="json")})


_service = AssetDeliveryService()


@router.post("/assets/{asset_revision_id}/delivery", response_model=AssetDeliveryResultV1)
def request_delivery(asset_revision_id: UUID, body: AssetDeliveryRequestV1, request: Request, db: DB):
    if body.asset_revision_id != asset_revision_id:
        raise HTTPException(status_code=400, detail="asset_revision_id in path and body must match")
    return _service.descriptor(db, asset_revision_id=asset_revision_id, variant=body.variant, host=request.headers.get("host", ""), base_url=str(request.base_url).rstrip("/"))


@router.post("/assets/{asset_revision_id}/delivery/alternate", response_model=AssetDeliveryResultV1)
def request_alternate(asset_revision_id: UUID, body: AlternateDeliveryRequestV1, request: Request, db: DB):
    if body.asset_revision_id != asset_revision_id:
        raise HTTPException(status_code=400, detail="asset_revision_id in path and body must match")
    return _service.alternate(db, body, host=request.headers.get("host", ""), base_url=str(request.base_url).rstrip("/"))


@router.post("/assets/{asset_revision_id}/delivery/diagnose", response_model=DiagnoseDeliveryResultV1)
def diagnose_delivery(asset_revision_id: UUID, body: DiagnoseDeliveryRequestV1, request: Request, db: DB):
    # The diagnostic token is bound to the revision in the process-local ledger.
    try:
        capability = _service.ledger.resolve_diagnostic(body.diagnostic_token, host=request.headers.get("host", ""))
    except DeliveryTokenError:
        return _service.diagnose(db, body, host=request.headers.get("host", ""))
    if capability.asset_revision_id != asset_revision_id:
        raise HTTPException(status_code=400, detail="diagnostic token does not match asset revision")
    return _service.diagnose(db, body, host=request.headers.get("host", ""))


@router.get("/asset-deliveries/{capability}")
def serve_delivery(capability: str, request: Request):
    return _service.serve(capability, request, host=request.headers.get("host", ""))



__all__ = [
    "AssetDeliveryService",
    "DeliveryConfigurationError",
    "DeliveryCapacityError",
    "DeliveryLimits",
    "DeliverySourceError",
    "router",
]
