from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from titles_api.platform_io import process_alive
from titles_api.storage.run_lease import RunLease
from titles_api.storage.leases import SupervisorLock
from titles_api.integrations.fal.credentials import FalSecretStore
from titles_api.integrations.llm.credentials import LlmSecretStore
from titles_api.storage.installation_backup import InstallationBackupStore


def test_process_probe_does_not_terminate_live_process():
    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        assert process_alive(process.pid)
        assert process.poll() is None
        assert not process_alive(0)
        process.communicate(timeout=5)
        assert not process_alive(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_lock_excludes_another_process_and_survives_metadata_reads(tmp_path):
    path = tmp_path / "runtime.lock"
    code = "from pathlib import Path; from titles_api.storage.run_lease import RunLease; import sys; RunLease.acquire(Path(sys.argv[1]))"
    # Explicit PYTHONPATH makes subprocess behavior identical on pytest runners.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    with RunLease.acquire(path, lease_id="first"):
        assert json.loads(path.read_text())["lease_id"] == "first"
        child = subprocess.run([sys.executable, "-c", code, str(path)], env=env, capture_output=True)
        assert child.returncode != 0
        assert b"RunLeaseBusy" in child.stderr
    child = subprocess.run([sys.executable, "-c", code, str(path)], env=env, capture_output=True)
    assert child.returncode == 0, child.stderr
    # OS releases the lock even though the child never called release().
    with RunLease.acquire(path):
        pass


def test_supervisor_lock_releases_for_next_owner(tmp_path):
    with SupervisorLock(tmp_path / "supervisor.lock") as first:
        first.assert_held()
    with SupervisorLock(tmp_path / "supervisor.lock") as second:
        second.assert_held()


def test_credential_roundtrip_under_unicode_path(tmp_path):
    for store in (FalSecretStore(tmp_path / "model café"), LlmSecretStore(tmp_path / "model café")):
        store.save("local-test-value")
        assert store.path.read_text() == "local-test-value"
        store.clear()
        assert not store.path.exists()


def test_backup_restore_closes_sqlite_handles(tmp_path):
    import sqlite3
    from contextlib import closing

    root = tmp_path / "model café"
    root.mkdir()
    with closing(sqlite3.connect(root / "modelfiche.sqlite3")) as db, db:
        db.execute("CREATE TABLE value (name TEXT)")
        db.execute("INSERT INTO value VALUES ('retained')")
    (root / "assets").mkdir()
    (root / "assets" / "sample").write_bytes(b"durable bytes")
    store = InstallationBackupStore(tmp_path / "backups")
    receipt = store.create(root)
    (root / "assets" / "sample").write_bytes(b"changed")
    store.restore(receipt.archive, root)
    assert (root / "assets" / "sample").read_bytes() == b"durable bytes"
    with closing(sqlite3.connect(root / "modelfiche.sqlite3")) as db:
        assert db.execute("SELECT name FROM value").fetchone()[0] == "retained"
