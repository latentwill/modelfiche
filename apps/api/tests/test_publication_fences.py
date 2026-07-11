from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from titles_api.storage.leases import LeaseFenceError, LeaseRegistry, SupervisorLock
from titles_api.storage.local_root import LocalRoot, LocalRootSafetyError
from titles_api.storage.publication import LocalPublisher


def test_publication_commits_receipt_and_effect_epoch_together(tmp_path: Path) -> None:
    root = LocalRoot.open(tmp_path / "assets")
    leases = LeaseRegistry()
    supervisor_lock = SupervisorLock(tmp_path / "supervisor.lock")
    supervisor_lock.acquire()
    claim = leases.claim("transfer-1", owner="worker-a", now=0, ttl_seconds=30)
    payload = b"generated-image-bytes"
    committed: list[tuple[str, int]] = []

    def commit(receipt, set_effect_epoch) -> None:
        set_effect_epoch()
        committed.append((receipt.attempt_id, receipt.effect_epoch))

    try:
        receipt = LocalPublisher(root, leases, supervisor_lock).publish(
            stream=io.BytesIO(payload),
            relative_path="attempts/attempt-1/content",
            attempt_id="attempt-1",
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            lease=claim,
            now=lambda: 1,
            commit_receipt=commit,
        )

        assert root.read_bytes(receipt.relative_path) == payload
        assert receipt.effect_epoch == 1
        assert committed == [("attempt-1", 1)]
    finally:
        supervisor_lock.close()
        root.close()


def test_stale_publication_fence_cannot_create_final_file(tmp_path: Path) -> None:
    root = LocalRoot.open(tmp_path / "assets")
    leases = LeaseRegistry()
    supervisor_lock = SupervisorLock(tmp_path / "supervisor.lock")
    supervisor_lock.acquire()
    stale = leases.claim("transfer-1", owner="worker-a", now=0, ttl_seconds=1)
    leases.claim("transfer-1", owner="worker-b", now=2, ttl_seconds=30)
    payload = b"stale"

    try:
        with pytest.raises(LeaseFenceError):
            LocalPublisher(root, leases, supervisor_lock).publish(
                stream=io.BytesIO(payload),
                relative_path="attempts/stale/content",
                attempt_id="stale",
                expected_size=len(payload),
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                lease=stale,
                now=lambda: 2,
                commit_receipt=lambda _receipt, _set_effect_epoch: None,
            )
        with pytest.raises(LocalRootSafetyError, match="does not exist"):
            root.read_bytes("attempts/stale/content")
    finally:
        supervisor_lock.close()
        root.close()
