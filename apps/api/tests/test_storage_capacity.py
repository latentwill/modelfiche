from __future__ import annotations

from pathlib import Path

import pytest

from titles_api.storage.capacity import CapacityExceeded, CapacityLedger, ReservationReapRequired


def test_reservation_is_not_released_until_the_writer_is_reaped(tmp_path: Path) -> None:
    root = tmp_path / "assets"
    root.mkdir()
    ledger = CapacityLedger(
        root_quotas={root: 10},
        temporary_quota_bytes=4,
        shared_headroom_bytes=0,
        disk_free=lambda _path: 100,
    )
    reservation = ledger.reserve(
        reservation_id="transfer-1",
        root=root,
        permanent_bytes=8,
        temporary_bytes=2,
        source_id="source-a",
    )

    ledger.mark_reclaim_pending(reservation.reservation_id)
    with pytest.raises(ReservationReapRequired):
        ledger.release(reservation.reservation_id, writer_reaped=False)
    with pytest.raises(CapacityExceeded):
        ledger.reserve(
            reservation_id="transfer-2",
            root=root,
            permanent_bytes=1,
            temporary_bytes=0,
            source_id="source-a",
        )

    ledger.release(reservation.reservation_id, writer_reaped=True)
    replacement = ledger.reserve(
        reservation_id="transfer-2",
        root=root,
        permanent_bytes=1,
        temporary_bytes=0,
        source_id="source-a",
    )
    assert replacement.permanent_bytes == 1


def test_roots_on_the_same_filesystem_share_headroom(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    ledger = CapacityLedger(
        root_quotas={root_a: 80, root_b: 80},
        temporary_quota_bytes=80,
        shared_headroom_bytes=30,
        disk_free=lambda _path: 100,
    )
    ledger.reserve(
        reservation_id="a",
        root=root_a,
        permanent_bytes=40,
        temporary_bytes=0,
        source_id="source-a",
    )

    with pytest.raises(CapacityExceeded, match="headroom"):
        ledger.reserve(
            reservation_id="b",
            root=root_b,
            permanent_bytes=31,
            temporary_bytes=0,
            source_id="source-b",
        )

def test_settings_exposes_numeric_root_and_headroom_limits_without_paths() -> None:
    from titles_api.settings import Settings

    limits = Settings().storage_limits()

    assert limits.asset_root_quota_bytes == 100 * 1024**3
    assert limits.cache_root_quota_bytes == 10 * 1024**3
    assert limits.temporary_quota_bytes == 10 * 1024**3
    assert limits.shared_headroom(50 * 1024**3) == 10 * 1024**3
    assert limits.shared_headroom(200 * 1024**3) == 20 * 1024**3


def test_settings_creates_owner_only_storage_roots(tmp_path: Path) -> None:
    from titles_api.settings import Settings

    settings = Settings(
        asset_root=tmp_path / "assets",
        cache_root=tmp_path / "cache",
        export_root=tmp_path / "exports",
    )
    settings.ensure_runtime_dirs()

    assert settings.asset_root.stat().st_mode & 0o777 == 0o700
    assert settings.cache_root.stat().st_mode & 0o777 == 0o700
