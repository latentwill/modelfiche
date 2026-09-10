from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from ..platform_io import fsync_directory as _fsync_directory
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
from contextlib import closing
import tarfile
import tempfile
import uuid

from .database_set import DatabaseSetRestorer

_CHUNK_SIZE = 8 * 1024 * 1024
_MANIFEST = "manifest.json"


@dataclass(frozen=True, slots=True)
class InstallationBackup:
    backup_id: str
    archive: Path
    created_at: str
    files: int
    bytes: int
    sha256: str


class InstallationBackupStore:
    """Verified, restorable snapshots of one local Modelfiche data directory."""

    def __init__(self, backup_root: Path) -> None:
        self.backup_root = backup_root.expanduser().resolve()
        self.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.backup_root, 0o700)

    def create(
        self,
        data_root: Path,
        database_name: str = "modelfiche.sqlite3",
        *,
        database_only: bool = False,
    ) -> InstallationBackup:
        data_root = data_root.expanduser().resolve()
        if self.backup_root == data_root or self.backup_root.is_relative_to(data_root):
            raise ValueError("backup directory must be outside the application data directory")
        database = data_root / database_name
        if not database.is_file():
            raise FileNotFoundError(f"database does not exist: {database}")
        backup_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        archive = self.backup_root / f"modelfiche-{created_at[:10]}-{backup_id}.tar.gz"
        partial = archive.with_suffix(archive.suffix + ".partial")
        with tempfile.TemporaryDirectory(prefix="mfiche-backup-", dir=self.backup_root) as temporary:
            stage = Path(temporary) / "data"
            stage.mkdir()
            if not database_only:
                self._copy_data_root(data_root, stage, database_name)
            self._sqlite_backup(database, stage / database_name)
            entries = self._manifest_entries(stage)
            manifest: dict[str, object] = {
                "version": 2 if database_only else 1,
                "backup_id": backup_id,
                "created_at": created_at,
                "database_name": database_name,
                "files": entries,
            }
            if database_only:
                manifest["scope"] = "database_only"
            (stage / _MANIFEST).write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            try:
                with tarfile.open(partial, "x:gz") as output:
                    for path in sorted(stage.rglob("*")):
                        output.add(path, arcname=path.relative_to(stage).as_posix(), recursive=False)
                _fsync_file(partial)
                os.replace(partial, archive)
                _fsync_directory(self.backup_root)
            finally:
                partial.unlink(missing_ok=True)
        verified = self.verify(archive)
        return InstallationBackup(backup_id, archive, created_at, len(entries), sum(int(row["size"]) for row in entries), verified)

    def verify(self, archive: Path) -> str:
        archive = archive.expanduser().resolve()
        with tempfile.TemporaryDirectory(prefix="mfiche-verify-", dir=self.backup_root) as temporary:
            stage = Path(temporary)
            self._extract(archive, stage)
            self._verify_stage(stage)
        return _sha256(archive)

    def restore(self, archive: Path, data_root: Path) -> None:
        archive = archive.expanduser().resolve()
        data_root = data_root.expanduser().resolve()
        parent = data_root.parent
        restore_id = str(uuid.uuid4())
        stage = parent / f".{data_root.name}.{restore_id}.restore"
        quarantine = parent / f".{data_root.name}.{restore_id}.previous"
        journal = parent / f".{data_root.name}.restore-journal.json"
        if journal.exists():
            raise RuntimeError(f"restore already in progress: {journal}")
        stage.mkdir(mode=0o700)
        try:
            self._extract(archive, stage)
            manifest = self._verify_stage(stage)
            if manifest.get("scope") == "database_only":
                raise ValueError(
                    "database-only backup cannot be restored as a full installation directory; "
                    "restore the database separately"
                )
            _write_json(journal, {"version": 1, "data_root": str(data_root), "stage": str(stage), "quarantine": str(quarantine), "archive_sha256": _sha256(archive)})
            if data_root.exists():
                os.replace(data_root, quarantine)
            os.replace(stage, data_root)
            _fsync_directory(parent)
            database_name = str(manifest["database_name"])
            self._verify_sqlite(data_root / database_name)
            journal.unlink()
            _fsync_directory(parent)
        except BaseException:
            if quarantine.exists():
                if data_root.exists():
                    failed = parent / f".{data_root.name}.{restore_id}.failed"
                    os.replace(data_root, failed)
                    shutil.rmtree(failed)
                os.replace(quarantine, data_root)
                journal.unlink(missing_ok=True)
                _fsync_directory(parent)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        if quarantine.exists():
            shutil.rmtree(quarantine)
            _fsync_directory(parent)

    def restore_database(self, archive: Path, database_path: Path) -> None:
        """Install a verified database-only archive into one SQLite database path."""
        archive = archive.expanduser().resolve()
        database_path = database_path.expanduser().resolve()
        with tempfile.TemporaryDirectory(prefix="mfiche-database-restore-", dir=database_path.parent) as temporary:
            stage = Path(temporary)
            self._extract(archive, stage)
            manifest = self._verify_stage(stage)
            if manifest.get("scope") != "database_only":
                raise ValueError("database restore requires a database-only backup")
            database_name = str(manifest["database_name"])
            DatabaseSetRestorer(database_path).replace(stage / database_name)


    @staticmethod
    def recover(data_root: Path) -> bool:
        """Roll back an interrupted directory swap to the pre-restore installation."""
        data_root = data_root.expanduser().resolve()
        journal = data_root.parent / f".{data_root.name}.restore-journal.json"
        if not journal.exists():
            return False
        state = json.loads(journal.read_text(encoding="utf-8"))
        if state.get("version") != 1 or state.get("data_root") != str(data_root):
            raise RuntimeError("restore journal does not match this installation")
        stage = Path(str(state["stage"]))
        quarantine = Path(str(state["quarantine"]))
        if quarantine.exists():
            if data_root.exists():
                failed = data_root.parent / f".{data_root.name}.{uuid.uuid4()}.failed"
                os.replace(data_root, failed)
                shutil.rmtree(failed)
            os.replace(quarantine, data_root)
        if stage.exists():
            shutil.rmtree(stage)
        journal.unlink()
        _fsync_directory(data_root.parent)
        return True


    @staticmethod
    def _copy_data_root(source: Path, destination: Path, database_name: str) -> None:
        excluded = {database_name, f"{database_name}-wal", f"{database_name}-shm"}
        for child in source.iterdir():
            if child.name in excluded or child.is_symlink():
                continue
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target, symlinks=False)
            elif child.is_file():
                shutil.copy2(child, target)

    @staticmethod
    def _sqlite_backup(source: Path, destination: Path) -> None:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as source_db, closing(sqlite3.connect(destination)) as target_db:
            source_db.backup(target_db, pages=256)
        InstallationBackupStore._verify_sqlite(destination)
        destination.with_name(f"{destination.name}-wal").unlink(missing_ok=True)
        destination.with_name(f"{destination.name}-shm").unlink(missing_ok=True)

    @staticmethod
    def _verify_sqlite(path: Path) -> None:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
            result = database.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise ValueError("backup database failed integrity check")

    @staticmethod
    def _manifest_entries(root: Path) -> list[dict[str, object]]:
        return [
            {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": _sha256(path)}
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.name != _MANIFEST
        ]

    @staticmethod
    def _extract(archive: Path, destination: Path) -> None:
        with tarfile.open(archive, "r:gz") as source:
            for member in source.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk():
                    raise ValueError("unsafe path in backup archive")
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = source.extractfile(member)
                    if stream is None:
                        raise ValueError("backup member cannot be read")
                    with target.open("xb") as output:
                        shutil.copyfileobj(stream, output)
                else:
                    raise ValueError("unsupported backup member type")

    @staticmethod
    def _verify_stage(stage: Path) -> dict[str, object]:
        manifest_path = stage / _MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_keys = {"version", "backup_id", "created_at", "database_name", "files"}
        if not isinstance(manifest, dict):
            raise ValueError("unsupported installation backup manifest")
        version = manifest.get("version")
        if version == 1 and set(manifest) == manifest_keys:
            scope = "full"
        elif (
            version == 2
            and set(manifest) == manifest_keys | {"scope"}
            and manifest.get("scope") == "database_only"
        ):
            scope = "database_only"
        else:
            raise ValueError("unsupported installation backup manifest")
        expected = {str(row["path"]): row for row in manifest["files"]}
        actual = {path.relative_to(stage).as_posix(): path for path in stage.rglob("*") if path.is_file() and path.name != _MANIFEST}
        if set(actual) != set(expected):
            raise ValueError("backup file inventory mismatch")
        for relative, path in actual.items():
            row = expected[relative]
            if path.stat().st_size != int(row["size"]) or _sha256(path) != row["sha256"]:
                raise ValueError(f"backup file integrity mismatch: {relative}")
        database_name = str(manifest["database_name"])
        if PurePosixPath(database_name).name != database_name:
            raise ValueError("invalid backup database name")
        if scope == "database_only" and set(actual) != {database_name}:
            raise ValueError("database-only backup must contain only its database")
        InstallationBackupStore._verify_sqlite(stage / database_name)
        return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())



