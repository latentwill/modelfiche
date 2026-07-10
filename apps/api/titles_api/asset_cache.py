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
    """Managed content cache with atomic writes and deterministic thumbnails."""

    def __init__(self, settings: CacheSettings):
        self.settings = settings
        self.root = settings.root
        self.objects = self.root / "objects"
        self.thumbnails = self.root / "thumbs"
        self.tmp = self.root / "tmp"
        for directory in (self.objects, self.thumbnails, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)

    def used_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.objects.rglob("*") if path.is_file())

    def ensure_capacity(self, incoming: int) -> None:
        if incoming < 0:
            raise ValueError("incoming size cannot be negative")
        free = shutil.disk_usage(self.root).free
        if incoming > free or self.used_bytes() + incoming > self.settings.max_bytes:
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
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temp_path.unlink()
            else:
                os.replace(temp_path, destination)
            return CacheEntry(destination, size, sha256)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def thumbnail(self, asset_id: str, source: Path, size: int = 512) -> Path:
        if size not in self.settings.thumbnail_sizes:
            raise ValueError(f"unsupported thumbnail size: {size}")
        destination = self.thumbnails / asset_id / f"{size}.webp"
        if destination.exists():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="thumb-", suffix=".webp", dir=destination.parent)
        os.close(fd)
        temp_path = Path(raw_path)
        try:
            from PIL import Image, ImageOps

            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((size, size), Image.Resampling.LANCZOS)
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGB")
                image.save(temp_path, "WEBP", quality=84, method=4)
            os.replace(temp_path, destination)
            return destination
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
