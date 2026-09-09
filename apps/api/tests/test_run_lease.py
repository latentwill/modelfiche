from __future__ import annotations

import json

import pytest

from titles_api.storage.run_lease import RunLease, RunLeaseBusy


def test_run_lease_excludes_a_second_runtime_and_clears_on_release(tmp_path) -> None:
    path = tmp_path / "runtime.lock"
    first = RunLease.acquire(path, lease_id="lease-a")
    assert json.loads(path.read_text()) ["lease_id"] == "lease-a"

    with pytest.raises(RunLeaseBusy):
        RunLease.acquire(path, lease_id="lease-b")

    first.release()
    with RunLease.acquire(path, lease_id="lease-b") as second:
        assert second.lease_id == "lease-b"
    assert path.read_text() == ""
