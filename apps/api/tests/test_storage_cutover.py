from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from titles_api.storage.backup import EnvelopeStore, FileKeyStore
from titles_api.storage.cutover import RestoreForbidden, assert_sanitized_restore_allowed
from titles_api.storage.database_set import DatabaseSetRestorer, InjectedCrash


def make_database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE state(value TEXT NOT NULL)")
        db.execute("INSERT INTO state VALUES(?)", (value,))


def read_value(path: Path) -> str:
    with sqlite3.connect(path) as db:
        return str(db.execute("SELECT value FROM state").fetchone()[0])


def test_encrypted_envelope_round_trip_and_plaintext_cleanup(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    make_database(source, "sanitized")
    store = EnvelopeStore(tmp_path / "backups", FileKeyStore(tmp_path / "keys"))

    receipt = store.create(source, kind="sanitized", bindings={"head": "0003", "marker": 1})
    restored = tmp_path / "restored.sqlite3"
    store.restore(receipt.backup_id, restored)

    assert read_value(restored) == "sanitized"
    assert not list((tmp_path / "backups").glob("*.plaintext"))
    assert receipt.sha256


def test_database_set_restore_never_pairs_new_main_with_old_sidecars(tmp_path: Path) -> None:
    installed = tmp_path / "titles.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    make_database(installed, "old")
    make_database(replacement, "new")
    Path(f"{installed}-wal").write_bytes(b"old-wal")
    Path(f"{installed}-shm").write_bytes(b"old-shm")

    restorer = DatabaseSetRestorer(installed)
    restorer.replace(replacement)

    assert read_value(installed) == "new"
    assert not Path(f"{installed}-wal").exists()
    assert not Path(f"{installed}-shm").exists()
    assert restorer.journal_path.exists() is False


def test_restore_resumes_after_old_set_quarantine(tmp_path: Path) -> None:
    installed = tmp_path / "titles.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    make_database(installed, "old")
    make_database(replacement, "new")
    restorer = DatabaseSetRestorer(installed)

    with pytest.raises(InjectedCrash):
        restorer.replace(replacement, crash_after="old_set_quarantined")

    restorer.resume()
    assert read_value(installed) == "new"
    assert not restorer.journal_path.exists()


def test_sanitized_restore_rejects_any_gate_or_external_effect() -> None:
    assert_sanitized_restore_allowed({"storage_contract": "disabled"}, effect_epoch=0, effect_rows=0)

    with pytest.raises(RestoreForbidden, match="after enable"):
        assert_sanitized_restore_allowed({"storage_contract": "ready"}, effect_epoch=0, effect_rows=0)
    with pytest.raises(RestoreForbidden, match="after enable"):
        assert_sanitized_restore_allowed({"storage_contract": "disabled"}, effect_epoch=1, effect_rows=1)
