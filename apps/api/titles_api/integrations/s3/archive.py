from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
import hashlib
import tarfile

from .models import Inventory, ObjectInfo


class TarArchiveBrowser:
    """Expose safe regular-file tar members through the importer browser contract."""

    def __init__(self, archive_path: str, archive_key: str, archive_etag: str):
        self.archive_key = archive_key
        self.prefix = archive_key + "!/"
        self._tar = tarfile.open(archive_path, mode="r:*")
        self._members: dict[str, tarfile.TarInfo] = {}
        for member in self._tar.getmembers():
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts:
                continue
            key = self.prefix + str(path)
            self._members[key] = member
        self._etag = archive_etag

    def close(self) -> None:
        self._tar.close()

    def inventory(self, prefix: str) -> Inventory:
        if prefix != self.prefix:
            raise ValueError("archive importer received an unexpected virtual prefix")
        items = [
            ObjectInfo(
                key=key,
                size=member.size,
                etag=hashlib.sha256(f"{self._etag}:{member.offset_data}:{member.size}".encode()).hexdigest(),
                modified_at=datetime.fromtimestamp(member.mtime, timezone.utc) if member.mtime else None,
            )
            for key, member in self._members.items()
        ]
        return Inventory(prefix=self.prefix, objects=items)

    def read_small(self, key: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        member = self._members[key]
        if member.size > max_bytes:
            raise ValueError(f"archive member is too large for metadata read ({member.size} bytes)")
        stream = self._tar.extractfile(member)
        if stream is None:
            raise ValueError("archive member has no readable body")
        return stream.read(max_bytes + 1)

    def copy_member(self, key: str, target) -> None:
        member = self._members[key]
        stream = self._tar.extractfile(member)
        if stream is None:
            raise ValueError("archive member has no readable body")
        while chunk := stream.read(1024 * 1024):
            target.write(chunk)

