from __future__ import annotations
import os
from pathlib import Path

from dataclasses import dataclass
from threading import RLock

from ..platform_io import lock_file


class LeaseFenceError(RuntimeError):
    """A worker no longer owns the lease generation it is attempting to use."""



class SupervisorLock:
    """Process-held installation lock required for every Local publication."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd = -1

    def acquire(self) -> None:
        if self._fd >= 0:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_file(fd)
        except OSError as exc:
            os.close(fd)
            raise LeaseFenceError("installation supervisor lock is held by another process") from exc
        self._fd = fd

    def assert_held(self) -> None:
        if self._fd < 0:
            raise LeaseFenceError("Local publication requires the installation supervisor lock")

    def close(self) -> None:
        if self._fd >= 0:
            try:
                lock_file(self._fd, unlock=True)
            finally:
                os.close(self._fd)
                self._fd = -1

    def __enter__(self) -> "SupervisorLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

@dataclass(frozen=True, slots=True)
class LeaseClaim:
    key: str
    owner: str
    generation: int
    expires_at: float


@dataclass(slots=True)
class _LeaseState:
    owner: str
    generation: int
    expires_at: float
    active: bool = True


class LeaseRegistry:
    """In-process CAS model for owner/generation-fenced worker leases.

    Persistent callers use the same owner/generation predicates in their short
    SQLite transactions; this registry keeps filesystem operations testable and
    makes a stale worker fail before every bounded publication chunk.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._leases: dict[str, _LeaseState] = {}

    def claim(self, key: str, *, owner: str, now: float, ttl_seconds: float) -> LeaseClaim:
        if not key or not owner:
            raise ValueError("lease key and owner are required")
        if ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive")
        with self._lock:
            current = self._leases.get(key)
            if current is not None and current.active and current.expires_at > now:
                raise LeaseFenceError("lease is still owned by an active worker")
            generation = 1 if current is None else current.generation + 1
            state = _LeaseState(owner=owner, generation=generation, expires_at=now + ttl_seconds)
            self._leases[key] = state
            return LeaseClaim(key=key, owner=owner, generation=generation, expires_at=state.expires_at)

    def assert_active(self, claim: LeaseClaim, *, now: float) -> None:
        with self._lock:
            state = self._leases.get(claim.key)
            if (
                state is None
                or not state.active
                or state.owner != claim.owner
                or state.generation != claim.generation
                or state.expires_at <= now
            ):
                raise LeaseFenceError("lease owner or generation is stale")

    def heartbeat(self, claim: LeaseClaim, *, now: float, ttl_seconds: float) -> LeaseClaim:
        if ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive")
        with self._lock:
            self.assert_active(claim, now=now)
            state = self._leases[claim.key]
            state.expires_at = now + ttl_seconds
            return LeaseClaim(claim.key, claim.owner, claim.generation, state.expires_at)

    def release(self, claim: LeaseClaim, *, now: float) -> None:
        with self._lock:
            self.assert_active(claim, now=now)
            self._leases[claim.key].active = False
