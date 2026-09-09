from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock


class CapacityExceeded(RuntimeError):
    pass


class ReservationReapRequired(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CapacityReservation:
    reservation_id: str
    root: Path
    filesystem_device: int
    permanent_bytes: int
    temporary_bytes: int
    source_id: str | None
    state: str

    @property
    def total_bytes(self) -> int:
        return self.permanent_bytes + self.temporary_bytes


class CapacityLedger:
    """Thread-safe reservation accounting for roots sharing one filesystem.

    The durable storage repository records the same grants before external I/O.
    This focused ledger owns the invariant that an expired grant remains counted
    until the physical writer has been reaped.
    """

    def __init__(
        self,
        *,
        root_quotas: Mapping[Path, int],
        temporary_quota_bytes: int,
        shared_headroom_bytes: int,
        disk_free: Callable[[Path], int] | None = None,
        global_transfer_limit: int = 4,
        per_source_transfer_limit: int = 2,
    ) -> None:
        if temporary_quota_bytes < 0 or shared_headroom_bytes < 0:
            raise ValueError("capacity limits cannot be negative")
        if global_transfer_limit <= 0 or per_source_transfer_limit <= 0:
            raise ValueError("transfer limits must be positive")
        normalized: dict[Path, int] = {}
        for root, quota in root_quotas.items():
            if quota < 0:
                raise ValueError("root quotas cannot be negative")
            normalized[_root_key(root)] = quota
        self._root_quotas = normalized
        self._temporary_quota_bytes = temporary_quota_bytes
        self._shared_headroom_bytes = shared_headroom_bytes
        self._disk_free = disk_free or (lambda path: os.statvfs(path).f_bavail * os.statvfs(path).f_frsize)
        self._global_transfer_limit = global_transfer_limit
        self._per_source_transfer_limit = per_source_transfer_limit
        self._reservations: dict[str, CapacityReservation] = {}
        self._lock = RLock()

    def reserve(
        self,
        *,
        reservation_id: str,
        root: Path,
        permanent_bytes: int,
        temporary_bytes: int,
        source_id: str | None,
    ) -> CapacityReservation:
        if not reservation_id:
            raise ValueError("reservation ID is required")
        if permanent_bytes < 0 or temporary_bytes < 0:
            raise ValueError("reservation byte counts cannot be negative")
        root_key = _root_key(root)
        with self._lock:
            if reservation_id in self._reservations:
                raise CapacityExceeded("reservation ID is already active")
            quota = self._root_quotas.get(root_key)
            if quota is None:
                raise CapacityExceeded("LocalRoot is not registered for capacity accounting")
            device = os.stat(root_key).st_dev
            requested = permanent_bytes + temporary_bytes
            active = tuple(self._reservations.values())
            if len(active) >= self._global_transfer_limit:
                raise CapacityExceeded("global background transfer limit reached")
            if source_id and sum(1 for item in active if item.source_id == source_id) >= self._per_source_transfer_limit:
                raise CapacityExceeded("per-source background transfer limit reached")
            root_used = sum(item.total_bytes for item in active if item.root == root_key)
            if root_used + requested > quota:
                raise CapacityExceeded("LocalRoot quota would be exceeded")
            temporary_used = sum(item.temporary_bytes for item in active)
            if temporary_used + temporary_bytes > self._temporary_quota_bytes:
                raise CapacityExceeded("temporary spool quota would be exceeded")
            filesystem_used = sum(item.total_bytes for item in active if item.filesystem_device == device)
            available_after_headroom = self._disk_free(root_key) - self._shared_headroom_bytes
            if filesystem_used + requested > available_after_headroom:
                raise CapacityExceeded("shared filesystem headroom would be exceeded")
            reservation = CapacityReservation(
                reservation_id=reservation_id,
                root=root_key,
                filesystem_device=device,
                permanent_bytes=permanent_bytes,
                temporary_bytes=temporary_bytes,
                source_id=source_id,
                state="active",
            )
            self._reservations[reservation_id] = reservation
            return reservation

    def assert_active(self, reservation_id: str) -> CapacityReservation:
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None or reservation.state != "active":
                raise CapacityExceeded("capacity reservation is not active")
            return reservation

    def mark_reclaim_pending(self, reservation_id: str) -> CapacityReservation:
        with self._lock:
            reservation = self.assert_active(reservation_id)
            pending = CapacityReservation(
                reservation_id=reservation.reservation_id,
                root=reservation.root,
                filesystem_device=reservation.filesystem_device,
                permanent_bytes=reservation.permanent_bytes,
                temporary_bytes=reservation.temporary_bytes,
                source_id=reservation.source_id,
                state="reclaim_pending",
            )
            self._reservations[reservation_id] = pending
            return pending

    def release(self, reservation_id: str, *, writer_reaped: bool) -> None:
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                return
            if not writer_reaped:
                raise ReservationReapRequired("capacity reservation remains held until writer/FD reap")
            del self._reservations[reservation_id]

    def reservation(self, reservation_id: str) -> CapacityReservation | None:
        with self._lock:
            return self._reservations.get(reservation_id)


def _root_key(root: Path) -> Path:
    return Path(os.path.abspath(root))
