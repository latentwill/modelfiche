from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .integrations.config import CacheSettings


@dataclass(frozen=True, slots=True)
class CacheEntry:
    path: Path
    size: int
    sha256: str


class CacheCapacityError(RuntimeError):
    pass


class AssetCache:
    """Compatibility facade over bounded content and derivative cache domains."""

    thumbnail_source_limit_bytes = 256 * 1024**2
    thumbnail_temporary_limit_bytes = 256 * 1024**2
    thumbnail_output_limit_bytes = 16 * 1024**2

    def __init__(self, settings: CacheSettings):
        from .settings import get_settings
        from .storage.cache import ThumbnailCache

        self.settings = settings
        self.root = settings.root
        self.objects = self.root / "objects"
        configured_limit = get_settings().storage_limits().cache_root_quota_bytes
        self.max_bytes = min(settings.max_bytes, configured_limit)
        self.thumbnail_cache = ThumbnailCache(self.root, quota_bytes=self.max_bytes)
        self.thumbnails = self.thumbnail_cache.objects
        self.tmp = self.thumbnail_cache.tmp
        self.objects.mkdir(parents=True, exist_ok=True, mode=0o700)

    def used_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    def ensure_capacity(self, incoming: int) -> None:
        if incoming < 0:
            raise ValueError("incoming size cannot be negative")
        free = shutil.disk_usage(self.root).free
        if incoming > free or self.used_bytes() + incoming > self.max_bytes:
            raise CacheCapacityError(f"insufficient cache capacity for {incoming} bytes")

    def hydrate(
        self,
        expected_size: int,
        writer: Callable[[BinaryIO], None],
        *,
        expected_sha256: str | None = None,
    ) -> CacheEntry:
        self.ensure_capacity(expected_size)
        fd, raw_path = tempfile.mkstemp(prefix="download-", dir=self.tmp)
        temp_path = Path(raw_path)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "w+b") as stream:
                writer(stream)
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if size != expected_size:
                raise ValueError(f"download size mismatch: expected {expected_size}, received {size}")
            sha256 = digest.hexdigest()
            if expected_sha256 and sha256.lower() != expected_sha256.lower():
                raise ValueError("download checksum mismatch")
            destination = self.objects / sha256[:2] / sha256
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.exists():
                temp_path.unlink()
            else:
                os.replace(temp_path, destination)
                _fsync_directory(destination.parent)
            return CacheEntry(destination, size, sha256)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def thumbnail(self, asset_id: str, source: Path, size: int = 512) -> Path:
        from .storage.cache import ThumbnailCapacityError

        if size not in self.settings.thumbnail_sizes:
            raise ValueError(f"unsupported thumbnail size: {size}")
        if source.stat().st_size > self.thumbnail_source_limit_bytes:
            raise CacheCapacityError("thumbnail source exceeds 256 MiB")
        key = hashlib.sha256(f"{asset_id}:{size}".encode("utf-8")).hexdigest()
        try:
            cached = self.thumbnail_cache.acquire(key)
        except KeyError:
            cached = None
        else:
            self.thumbnail_cache.release(key)
            return cached
        try:
            grant = self.thumbnail_cache.reserve_thumbnail(
                key,
                source_bytes=0,
                temporary_bytes=self.thumbnail_temporary_limit_bytes,
                output_bytes=self.thumbnail_output_limit_bytes,
            )
        except ThumbnailCapacityError as exc:
            raise CacheCapacityError(str(exc)) from exc
        fd, raw_path = tempfile.mkstemp(prefix="thumb-", suffix=".webp", dir=self.tmp)
        os.close(fd)
        temp_path = Path(raw_path)
        committed = False
        try:
            from PIL import Image, ImageOps

            with Image.open(source) as image:
                width, height = image.size
                if width > 32_768 or height > 32_768 or width * height > 100_000_000:
                    raise CacheCapacityError("thumbnail source dimensions exceed decode limits")
                image = ImageOps.exif_transpose(image)
                image.thumbnail((size, size), Image.Resampling.LANCZOS)
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGB")
                image.save(temp_path, "WEBP", quality=84, method=4)
            if temp_path.stat().st_size > self.thumbnail_output_limit_bytes:
                raise CacheCapacityError("thumbnail output exceeds 16 MiB")
            content = temp_path.read_bytes()
            temp_path.unlink()
            result = self.thumbnail_cache.commit_thumbnail(grant.grant_id, content)
            committed = True
            return result
        except ThumbnailCapacityError as exc:
            raise CacheCapacityError(str(exc)) from exc
        finally:
            temp_path.unlink(missing_ok=True)
            if not committed:
                self.thumbnail_cache.release_thumbnail(grant.grant_id)




def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
