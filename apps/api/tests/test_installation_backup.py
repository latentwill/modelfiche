from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tarfile

import pytest

from titles_api.storage.installation_backup import InstallationBackupStore


def make_database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE state (value TEXT NOT NULL)")
        database.execute("INSERT INTO state VALUES (?)", (value,))


def database_value(path: Path) -> str:
    with sqlite3.connect(path) as database:
        return str(database.execute("SELECT value FROM state").fetchone()[0])


def test_create_verify_and_restore_complete_installation(tmp_path: Path) -> None:
    data_root = tmp_path / "Modelfiche"
    data_root.mkdir()
    make_database(data_root / "modelfiche.sqlite3", "before")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "sample.png").write_bytes(b"asset-content")
    (data_root / "cache").mkdir()
    (data_root / "cache" / "thumbnail.jpg").write_bytes(b"cache-content")
    store = InstallationBackupStore(tmp_path / "backups")

    receipt = store.create(data_root)
    digest = store.verify(receipt.archive)

    assert digest == receipt.sha256
    assert receipt.files == 3
    with sqlite3.connect(data_root / "modelfiche.sqlite3") as database:
        database.execute("UPDATE state SET value = 'after'")
    (data_root / "assets" / "sample.png").write_bytes(b"changed")
    (data_root / "new-file").write_text("not in backup", encoding="utf-8")

    store.restore(receipt.archive, data_root)

    assert database_value(data_root / "modelfiche.sqlite3") == "before"
    assert (data_root / "assets" / "sample.png").read_bytes() == b"asset-content"
    assert (data_root / "cache" / "thumbnail.jpg").read_bytes() == b"cache-content"
    assert not (data_root / "new-file").exists()

def test_create_database_only_contains_consistent_database_and_manifest(tmp_path: Path) -> None:
    data_root = tmp_path / "Modelfiche"
    data_root.mkdir()
    make_database(data_root / "modelfiche.sqlite3", "before")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "sample.png").write_bytes(b"asset-content")
    (data_root / "cache").mkdir()
    (data_root / "cache" / "thumbnail.jpg").write_bytes(b"cache-content")
    store = InstallationBackupStore(tmp_path / "backups")

    receipt = store.create(data_root, database_only=True)

    assert receipt.files == 1
    assert store.verify(receipt.archive) == receipt.sha256
    with tarfile.open(receipt.archive, "r:gz") as archive:
        assert set(archive.getnames()) == {"manifest.json", "modelfiche.sqlite3"}
        manifest = json.loads(archive.extractfile("manifest.json").read())
    assert manifest["version"] == 2
    assert manifest["scope"] == "database_only"
    assert [row["path"] for row in manifest["files"]] == ["modelfiche.sqlite3"]



def test_restore_database_installs_database_only_archive(tmp_path: Path) -> None:
    data_root = tmp_path / "Modelfiche"
    data_root.mkdir()
    database = data_root / "modelfiche.sqlite3"
    make_database(database, "before")
    store = InstallationBackupStore(tmp_path / "backups")
    receipt = store.create(data_root, database_only=True)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE state SET value = 'after'")

    restored = tmp_path / "restored.sqlite3"
    store.restore_database(receipt.archive, restored)

    assert database_value(restored) == "before"
    assert not Path(f"{restored}-wal").exists()
    assert not Path(f"{restored}-shm").exists()
    assert not restored.with_name(f".{restored.name}.restore-journal.json").exists()


def test_restore_database_rejects_full_archive(tmp_path: Path) -> None:
    data_root = tmp_path / "Modelfiche"
    data_root.mkdir()
    make_database(data_root / "modelfiche.sqlite3", "before")
    store = InstallationBackupStore(tmp_path / "backups")
    receipt = store.create(data_root)

    with pytest.raises(ValueError, match="database-only"):
        store.restore_database(receipt.archive, tmp_path / "restored.sqlite3")

def test_restore_rejects_database_only_archive_as_full_installation(tmp_path: Path) -> None:
    data_root = tmp_path / "Modelfiche"
    data_root.mkdir()
    make_database(data_root / "modelfiche.sqlite3", "before")
    (data_root / "assets").mkdir()
    (data_root / "assets" / "sample.png").write_bytes(b"asset-content")
    store = InstallationBackupStore(tmp_path / "backups")
    receipt = store.create(data_root, database_only=True)

    with pytest.raises(ValueError, match="database-only.*full installation"):
        store.restore(receipt.archive, data_root)

    assert database_value(data_root / "modelfiche.sqlite3") == "before"
    assert (data_root / "assets" / "sample.png").read_bytes() == b"asset-content"

def test_verify_rejects_truncated_archive(tmp_path: Path) -> None:
    data_root = tmp_path / "Modelfiche"
    data_root.mkdir()
    make_database(data_root / "modelfiche.sqlite3", "before")
    store = InstallationBackupStore(tmp_path / "backups")
    receipt = store.create(data_root)
    receipt.archive.write_bytes(receipt.archive.read_bytes()[:100])

    with pytest.raises((EOFError, OSError, ValueError)):
        store.verify(receipt.archive)


def test_recover_rolls_back_interrupted_directory_swap(tmp_path: Path) -> None:
    data_root = tmp_path / "Modelfiche"
    data_root.mkdir()
    (data_root / "state").write_text("old", encoding="utf-8")
    stage = tmp_path / ".Modelfiche.restore"
    stage.mkdir()
    (stage / "state").write_text("new", encoding="utf-8")
    quarantine = tmp_path / ".Modelfiche.previous"
    data_root.rename(quarantine)
    journal = tmp_path / ".Modelfiche.restore-journal.json"
    journal.write_text(json.dumps({"version": 1, "data_root": str(data_root), "stage": str(stage), "quarantine": str(quarantine)}), encoding="utf-8")

    assert InstallationBackupStore.recover(data_root) is True
    assert (data_root / "state").read_text(encoding="utf-8") == "old"
    assert not stage.exists()
    assert not journal.exists()
