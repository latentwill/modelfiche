from __future__ import annotations

import errno
import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator


class LocalRootSafetyError(RuntimeError):
    """A filesystem object no longer satisfies the LocalRoot contract."""


@dataclass(frozen=True, slots=True)
class RootFingerprint:
    device: int
    inode: int
    owner_uid: int
    mode: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "RootFingerprint":
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            owner_uid=value.st_uid,
            mode=stat.S_IMODE(value.st_mode),
        )


class LocalRoot:
    """An owner-only root accessed exclusively through a retained directory FD."""

    def __init__(self, path: Path, directory_fd: int, fingerprint: RootFingerprint) -> None:
        self.path = path
        self._directory_fd = directory_fd
        self.fingerprint = fingerprint

    @classmethod
    def open(cls, path: Path, *, owner_uid: int | None = None, create: bool = True) -> "LocalRoot":
        configured_path = Path(os.path.abspath(path))
        if create:
            configured_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(configured_path, flags)
        except OSError as exc:
            raise _local_root_error(exc, configured_path) from exc
        try:
            root = cls(configured_path, directory_fd, RootFingerprint.from_stat(os.fstat(directory_fd)))
            root._validate_directory(os.fstat(directory_fd), expected_owner=owner_uid)
            root.assert_current()
            return root
        except BaseException:
            os.close(directory_fd)
            raise

    def close(self) -> None:
        if self._directory_fd >= 0:
            os.close(self._directory_fd)
            self._directory_fd = -1

    def __enter__(self) -> "LocalRoot":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def assert_current(self) -> None:
        self._ensure_open()
        try:
            configured = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise _local_root_error(exc, self.path) from exc
        if stat.S_ISLNK(configured.st_mode):
            raise LocalRootSafetyError("configured LocalRoot is a symlink")
        current = RootFingerprint.from_stat(configured)
        retained = RootFingerprint.from_stat(os.fstat(self._directory_fd))
        if current != self.fingerprint or retained != self.fingerprint:
            raise LocalRootSafetyError("configured LocalRoot was replaced")
        self._validate_directory(configured)

    @contextmanager
    def open_parent(self, relative_path: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = _relative_parts(relative_path)
        if len(parts) < 1:
            raise LocalRootSafetyError("LocalRoot path must name a file")
        self.assert_current()
        fd = os.dup(self._directory_fd)
        try:
            for part in parts[:-1]:
                fd = self._open_child_directory(fd, part, create=create)
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def create_exclusive(self, relative_path: str, *, mode: int = 0o600) -> int:
        with self.open_parent(relative_path, create=True) as (parent_fd, name):
            try:
                return os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    mode,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise _local_root_error(exc, self.path / relative_path) from exc

    def open_checked(self, relative_path: str, flags: int = os.O_RDONLY) -> int:
        with self.open_parent(relative_path) as (parent_fd, name):
            self._validate_entry(parent_fd, name, relative_path)
            try:
                fd = os.open(name, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            except OSError as exc:
                raise _local_root_error(exc, self.path / relative_path) from exc
        try:
            self._validate_file(os.fstat(fd), relative_path)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read_bytes(self, relative_path: str) -> bytes:
        fd = self.open_checked(relative_path)
        try:
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(fd)

    def verify_file(self, relative_path: str, *, expected_size: int, expected_sha256: str) -> None:
        if expected_size < 0:
            raise ValueError("expected_size cannot be negative")
        fd = self.open_checked(relative_path)
        try:
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "rb", closefd=False) as stream:
                while chunk := stream.read(8 * 1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if size != expected_size:
                raise LocalRootSafetyError("LocalRoot file size verification failed")
            if digest.hexdigest().lower() != expected_sha256.lower():
                raise LocalRootSafetyError("LocalRoot file checksum verification failed")
        finally:
            os.close(fd)

    def link_no_replace(self, source_relative_path: str, destination_relative_path: str) -> None:
        with self.open_parent(source_relative_path) as (source_parent, source_name):
            self._validate_entry(source_parent, source_name, source_relative_path)
            with self.open_parent(destination_relative_path, create=True) as (destination_parent, destination_name):
                try:
                    os.link(
                        source_name,
                        destination_name,
                        src_dir_fd=source_parent,
                        dst_dir_fd=destination_parent,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        raise LocalRootSafetyError("LocalRoot final path already exists") from exc
                    raise _local_root_error(exc, self.path / destination_relative_path) from exc
                os.fsync(destination_parent)

    def unlink(self, relative_path: str, *, missing_ok: bool = True) -> None:
        with self.open_parent(relative_path) as (parent_fd, name):
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                if not missing_ok:
                    raise
            os.fsync(parent_fd)

    def fsync_parent(self, relative_path: str) -> None:
        with self.open_parent(relative_path) as (parent_fd, _):
            os.fsync(parent_fd)

    def _open_child_directory(self, parent_fd: int, part: str, *, create: bool) -> int:
        try:
            entry = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(part, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            entry = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(entry.st_mode):
            raise LocalRootSafetyError("LocalRoot path contains a symlink")
        self._validate_directory(entry)
        try:
            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        except OSError as exc:
            raise _local_root_error(exc, self.path / part) from exc
        os.close(parent_fd)
        return child_fd

    def _validate_entry(self, parent_fd: int, name: str, relative_path: str) -> None:
        try:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise _local_root_error(exc, self.path / relative_path) from exc
        if stat.S_ISLNK(entry.st_mode):
            raise LocalRootSafetyError("LocalRoot path contains a symlink")
        self._validate_file(entry, relative_path)

    def _validate_directory(self, value: os.stat_result, *, expected_owner: int | None = None) -> None:
        if not stat.S_ISDIR(value.st_mode):
            raise LocalRootSafetyError("LocalRoot is not a directory")
        owner = os.geteuid() if expected_owner is None else expected_owner
        if value.st_uid != owner:
            raise LocalRootSafetyError("LocalRoot owner does not match process owner")
        if stat.S_IMODE(value.st_mode) & 0o077:
            raise LocalRootSafetyError("LocalRoot must be owner-only")

    def _validate_file(self, value: os.stat_result, relative_path: str) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise LocalRootSafetyError(f"LocalRoot path is not a regular file: {relative_path}")
        if value.st_uid != self.fingerprint.owner_uid:
            raise LocalRootSafetyError("LocalRoot file owner does not match root owner")
        if stat.S_IMODE(value.st_mode) & 0o077:
            raise LocalRootSafetyError("LocalRoot file must be owner-only")
        if value.st_nlink != 1:
            raise LocalRootSafetyError("LocalRoot file link count is not one")

    def _ensure_open(self) -> None:
        if self._directory_fd < 0:
            raise LocalRootSafetyError("LocalRoot is closed")


def _relative_parts(relative_path: str) -> tuple[str, ...]:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or not relative_path or "\\" in relative_path:
        raise LocalRootSafetyError("LocalRoot path must be a non-empty relative POSIX path")
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise LocalRootSafetyError("LocalRoot path traversal is forbidden")
    return parts


def _local_root_error(error: OSError, path: Path) -> LocalRootSafetyError:
    if error.errno == errno.ELOOP:
        return LocalRootSafetyError(f"LocalRoot path contains a symlink: {path}")
    if error.errno == errno.ENOENT:
        return LocalRootSafetyError(f"LocalRoot path does not exist: {path}")
    return LocalRootSafetyError(f"LocalRoot operation failed for {path}: {error.strerror}")
