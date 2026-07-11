from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Callable


class ThumbnailCapacityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ThumbnailGrant:
    grant_id: str
    key: str
    source_bytes: int
    temporary_bytes: int
    output_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.source_bytes + self.temporary_bytes + self.output_bytes


@dataclass(frozen=True, slots=True)
class _Entry:
    key: str
    path: Path
    size: int
    created_order: int
    delivered_order: int
    leases: int = 0


class ThumbnailCache:
    """A bounded derivative-only cache with deterministic unleased LRU eviction."""

    def __init__(
        self,
        root: Path,
        *,
        quota_bytes: int,
        headroom_bytes: int = 0,
        disk_free: Callable[[Path], int] | None = None,
    ) -> None:
        if quota_bytes < 0 or headroom_bytes < 0:
            raise ValueError("thumbnail cache limits cannot be negative")
        self.root = Path(root)
        self.objects = self.root / "thumbnails"
        self.tmp = self.root / "tmp"
        self.objects.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._quota_bytes = quota_bytes
        self._headroom_bytes = headroom_bytes
        self._disk_free = disk_free or (lambda path: os.statvfs(path).f_bavail * os.statvfs(path).f_frsize)
        self._entries: dict[str, _Entry] = {}
        self._grants: dict[str, ThumbnailGrant] = {}
        self._order = 0
        self._lock = RLock()

    def write_complete(self, key: str, content: bytes) -> Path:
        if not isinstance(content, bytes):
            raise TypeError("thumbnail content must be bytes")
        with self._lock:
            self._validate_key(key)
            old = self._entries.get(key)
            old_size = old.size if old is not None else 0
            self._make_room(len(content) - old_size)
            destination = self.objects / hashlib.sha256(key.encode("utf-8")).hexdigest()[:2] / (
                hashlib.sha256(key.encode("utf-8")).hexdigest() + ".webp"
            )
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, raw_temp = tempfile.mkstemp(prefix="thumb-", suffix=".tmp", dir=self.tmp)
            temp = Path(raw_temp)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, destination)
                _fsync_directory(destination.parent)
            finally:
                temp.unlink(missing_ok=True)
            self._order += 1
            self._entries[key] = _Entry(
                key=key,
                path=destination,
                size=len(content),
                created_order=self._order if old is None else old.created_order,
                delivered_order=self._order,
                leases=old.leases if old is not None else 0,
            )
            return destination

    def acquire(self, key: str) -> Path:
        with self._lock:
            try:
                entry = self._entry(key)
            except KeyError:
                entry = self._discover(key)
            self._order += 1
            entry = replace(entry, leases=entry.leases + 1, delivered_order=self._order)
            self._entries[key] = entry
            return entry.path

    def _discover(self, key: str) -> _Entry:
        """Recover a completed deterministic entry after a process/cache restart."""
        self._validate_key(key)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self.objects / digest[:2] / f"{digest}.webp"
        if not path.is_file():
            raise KeyError(key)
        self._order += 1
        entry = _Entry(key=key, path=path, size=path.stat().st_size, created_order=self._order, delivered_order=self._order)
        self._entries[key] = entry
        return entry

    def release(self, key: str) -> None:
        with self._lock:
            entry = self._entry(key)
            if entry.leases <= 0:
                raise RuntimeError("thumbnail entry is not leased")
            self._entries[key] = replace(entry, leases=entry.leases - 1)

    def reserve_thumbnail(
        self,
        key: str,
        *,
        source_bytes: int,
        temporary_bytes: int,
        output_bytes: int,
    ) -> ThumbnailGrant:
        if min(source_bytes, temporary_bytes, output_bytes) < 0:
            raise ValueError("thumbnail grant byte counts cannot be negative")
        with self._lock:
            self._validate_key(key)
            grant_id = hashlib.sha256(f"{key}:{self._order}:{len(self._grants)}".encode()).hexdigest()
            requested = source_bytes + temporary_bytes + output_bytes
            self._make_room(requested)
            grant = ThumbnailGrant(grant_id, key, source_bytes, temporary_bytes, output_bytes)
            self._grants[grant_id] = grant
            return grant

    def release_thumbnail(self, grant_id: str) -> None:
        with self._lock:
            self._grants.pop(grant_id, None)

    def commit_thumbnail(self, grant_id: str, content: bytes) -> Path:
        """Replace a pre-admitted output grant with its completed derivative."""
        with self._lock:
            grant = self._grants.pop(grant_id, None)
            if grant is None:
                raise KeyError(grant_id)
            if len(content) > grant.output_bytes:
                raise ThumbnailCapacityError("thumbnail output exceeds its admitted reservation")
            return self.write_complete(grant.key, content)

    def foreign_bytes(self) -> int:
        with self._lock:
            known_paths = {entry.path for entry in self._entries.values()}
            return sum(
                path.stat().st_size
                for path in self.root.rglob("*")
                if path.is_file() and path not in known_paths
            )

    def _make_room(self, requested: int) -> None:
        if requested < 0:
            return
        while self._used_bytes() + requested > self._available_capacity():
            candidates = [entry for entry in self._entries.values() if entry.leases == 0]
            if not candidates:
                raise ThumbnailCapacityError("thumbnail cache has no unleased entry to evict")
            victim = min(candidates, key=lambda entry: (entry.delivered_order, entry.created_order, entry.key))
            victim.path.unlink(missing_ok=True)
            _fsync_directory(victim.path.parent)
            del self._entries[victim.key]

    def _used_bytes(self) -> int:
        tracked = sum(entry.size for entry in self._entries.values())
        grants = sum(grant.total_bytes for grant in self._grants.values())
        return tracked + grants + self.foreign_bytes()

    def _available_capacity(self) -> int:
        return min(self._quota_bytes, max(0, self._disk_free(self.root) - self._headroom_bytes))

    def _entry(self, key: str) -> _Entry:
        entry = self._entries.get(key)
        if entry is None or not entry.path.is_file():
            self._entries.pop(key, None)
            raise KeyError(key)
        return entry

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key or "/" in key or "\\" in key or key in {".", ".."}:
            raise ValueError("thumbnail key must be an opaque non-path value")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
