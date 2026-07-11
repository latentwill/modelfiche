from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO
import hashlib
import hmac
import io
import tempfile
from pathlib import Path
from threading import Lock
from uuid import UUID

from PIL import Image, ImageOps, UnidentifiedImageError

from .cache import ThumbnailCache, ThumbnailCapacityError


class ThumbnailError(RuntimeError):
    """A source image cannot safely produce a derivative."""


@dataclass(frozen=True, slots=True)
class ThumbnailLimits:
    max_source_bytes: int = 256 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    max_dimension: int = 32_768
    max_pixels: int = 100_000_000
    max_decoded_bytes: int = 512 * 1024 * 1024
    chunk_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if min(
            self.max_source_bytes,
            self.max_output_bytes,
            self.max_dimension,
            self.max_pixels,
            self.max_decoded_bytes,
            self.chunk_bytes,
        ) <= 0:
            raise ValueError("thumbnail limits must be positive")


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    path: Path
    cache_key: str


class ThumbnailRenderer:
    """Build bounded, revision-keyed WebP derivatives in the cache capacity domain."""

    def __init__(
        self,
        cache: ThumbnailCache,
        *,
        limits: ThumbnailLimits | None = None,
        encoder_version: str = "webp-v1",
    ) -> None:
        if not encoder_version:
            raise ValueError("thumbnail encoder version is required")
        self._cache = cache
        self._limits = limits or ThumbnailLimits()
        self._encoder_version = encoder_version
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()

    def render(
        self,
        *,
        asset_revision_id: UUID,
        max_pixels: int,
        source: BinaryIO,
        source_size: int | None,
        expected_sha256: str | None,
    ) -> ThumbnailResult:
        if max_pixels not in {256, 512, 1024}:
            raise ThumbnailError("thumbnail variant is unsupported")
        if source_size is not None and (source_size < 0 or source_size > self._limits.max_source_bytes):
            raise ThumbnailError("thumbnail source exceeds configured bound")
        key = self.cache_key(asset_revision_id, max_pixels)
        with self._key_lock(key):
            try:
                return ThumbnailResult(self._cache.acquire(key), key)
            except KeyError:
                pass
            return self._render_new(
                key=key,
                max_pixels=max_pixels,
                source=source,
                expected_sha256=expected_sha256,
            )

    def release(self, result: ThumbnailResult) -> None:
        self._cache.release(result.cache_key)

    def cache_key(self, asset_revision_id: UUID, max_pixels: int) -> str:
        identity = f"{asset_revision_id}:{max_pixels}:{self._encoder_version}".encode("ascii")
        return hashlib.sha256(identity).hexdigest()

    def _render_new(
        self,
        *,
        key: str,
        max_pixels: int,
        source: BinaryIO,
        expected_sha256: str | None,
    ) -> ThumbnailResult:
        try:
            grant = self._cache.reserve_thumbnail(
                key,
                source_bytes=0,
                temporary_bytes=self._limits.max_source_bytes,
                output_bytes=self._limits.max_output_bytes,
            )
        except ThumbnailCapacityError as exc:
            raise ThumbnailError("thumbnail capacity exhausted") from exc

        committed = False
        try:
            with tempfile.TemporaryFile(mode="w+b", dir=self._cache.tmp) as temporary:
                source_digest = self._copy_bounded(source, temporary)
                if expected_sha256 is not None and not hmac.compare_digest(source_digest, expected_sha256.lower()):
                    raise ThumbnailError("thumbnail source checksum drifted")
                temporary.seek(0)
                content = self._encode_webp(temporary, max_pixels)
            path = self._cache.commit_thumbnail(grant.grant_id, content)
            committed = True
            return ThumbnailResult(self._cache.acquire(key), key)
        except ThumbnailCapacityError as exc:
            raise ThumbnailError("thumbnail capacity exhausted") from exc
        finally:
            if not committed:
                self._cache.release_thumbnail(grant.grant_id)

    def _copy_bounded(self, source: BinaryIO, temporary: BinaryIO) -> str:
        digest = hashlib.sha256()
        total = 0
        while chunk := source.read(self._limits.chunk_bytes):
            total += len(chunk)
            if total > self._limits.max_source_bytes:
                raise ThumbnailError("thumbnail source exceeds configured bound")
            temporary.write(chunk)
            digest.update(chunk)
        temporary.flush()
        return digest.hexdigest()

    def _encode_webp(self, source: BinaryIO, max_pixels: int) -> bytes:
        try:
            with Image.open(source) as image:
                width, height = image.size
                if width > self._limits.max_dimension or height > self._limits.max_dimension:
                    raise ThumbnailError("thumbnail source dimensions exceed configured bound")
                pixels = width * height
                if pixels > self._limits.max_pixels:
                    raise ThumbnailError("thumbnail source pixel count exceeds configured bound")
                if pixels * 4 > self._limits.max_decoded_bytes:
                    raise ThumbnailError("thumbnail decoded memory exceeds configured bound")
                image.load()
                normalized = ImageOps.exif_transpose(image)
                normalized.thumbnail((max_pixels, max_pixels), Image.Resampling.LANCZOS)
                if normalized.mode not in {"RGB", "RGBA"}:
                    normalized = normalized.convert("RGB")
                output = io.BytesIO()
                normalized.save(output, "WEBP", quality=84, method=4)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, MemoryError) as exc:
            raise ThumbnailError("thumbnail source cannot be decoded") from exc
        content = output.getvalue()
        if len(content) > self._limits.max_output_bytes:
            raise ThumbnailError("thumbnail output exceeds configured bound")
        return content

    def _key_lock(self, key: str) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, Lock())
