from __future__ import annotations

import hashlib
import json
import os
from ..platform_io import fsync_directory as _fsync_directory
from pathlib import Path
import shutil
import sqlite3
from contextlib import closing
import uuid


class InjectedCrash(RuntimeError):
    pass


class DatabaseSetRestorer:
    """Journaled replacement of SQLite main, WAL, and SHM as one authority set."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.journal_path = database_path.with_name(f".{database_path.name}.restore-journal.json")

    def replace(self, replacement: Path, *, crash_after: str | None = None) -> None:
        if self.journal_path.exists():
            raise RuntimeError("database-set restore already in progress")
        restore_id = str(uuid.uuid4())
        stage = self.database_path.with_name(f".{self.database_path.name}.{restore_id}.new")
        quarantine = self.database_path.with_name(f".{self.database_path.name}.{restore_id}.quarantine")
        shutil.copyfile(replacement, stage)
        _fsync_file(stage)
        journal = {
            "version": 1,
            "restore_id": restore_id,
            "database": str(self.database_path),
            "stage": str(stage),
            "stage_sha256": _sha256(stage),
            "quarantine": str(quarantine),
            "phase": "prepared",
        }
        self._write_journal(journal)
        self._continue(journal, crash_after=crash_after)

    def resume(self) -> None:
        if not self.journal_path.exists():
            return
        journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if journal.get("version") != 1 or journal.get("database") != str(self.database_path):
            raise RuntimeError("restore journal does not match installation")
        self._continue(journal, crash_after=None)

    def _continue(self, journal: dict[str, object], *, crash_after: str | None) -> None:
        stage = Path(str(journal["stage"]))
        quarantine = Path(str(journal["quarantine"]))
        phase = str(journal["phase"])

        if phase == "prepared":
            if not stage.exists() or _sha256(stage) != journal["stage_sha256"]:
                raise RuntimeError("restored database stage changed")
            self._checkpoint_if_possible()
            quarantine.mkdir(mode=0o700, exist_ok=False)
            for current in self._database_set_paths():
                if current.exists():
                    os.replace(current, quarantine / current.name)
            _fsync_directory(quarantine)
            _fsync_directory(self.database_path.parent)
            phase = "old_set_quarantined"
            journal["phase"] = phase
            self._write_journal(journal)
            self._crash(crash_after, phase)

        if phase == "old_set_quarantined":
            if not stage.exists() or _sha256(stage) != journal["stage_sha256"]:
                raise RuntimeError("restored database stage changed")
            os.replace(stage, self.database_path)
            _fsync_file(self.database_path)
            _fsync_directory(self.database_path.parent)
            phase = "main_installed"
            journal["phase"] = phase
            self._write_journal(journal)
            self._crash(crash_after, phase)

        if phase == "main_installed":
            for sidecar in self._database_set_paths()[1:]:
                sidecar.unlink(missing_ok=True)
            _fsync_directory(self.database_path.parent)
            self._verify_installed(str(journal["stage_sha256"]))
            phase = "validated"
            journal["phase"] = phase
            self._write_journal(journal)
            self._crash(crash_after, phase)

        if phase == "validated":
            self.journal_path.unlink(missing_ok=True)
            _fsync_directory(self.database_path.parent)

    def _checkpoint_if_possible(self) -> None:
        if not self.database_path.exists():
            return
        try:
            with closing(sqlite3.connect(self.database_path)) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.DatabaseError:
            # A corrupt/foreign sidecar must be quarantined, never replayed with the replacement.
            pass

    def _verify_installed(self, expected_sha256: str) -> None:
        if _sha256(self.database_path) != expected_sha256:
            raise RuntimeError("installed restored database digest mismatch")
        with closing(sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)) as db:
            result = db.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError("installed restored database failed integrity check")

    def _database_set_paths(self) -> tuple[Path, Path, Path]:
        return (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        )

    def _write_journal(self, journal: dict[str, object]) -> None:
        temp = self.journal_path.with_suffix(self.journal_path.suffix + ".tmp")
        payload = json.dumps(journal, sort_keys=True, separators=(",", ":")).encode("utf-8")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, self.journal_path)
        _fsync_directory(self.journal_path.parent)

    @staticmethod
    def _crash(expected: str | None, phase: str) -> None:
        if expected == phase:
            raise InjectedCrash(phase)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    # Windows _commit requires a writable file descriptor.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())
