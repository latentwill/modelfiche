from __future__ import annotations

import hashlib
import io
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .leases import LeaseClaim, LeaseRegistry, SupervisorLock
from .local_root import LocalRoot, LocalRootSafetyError


MAX_PUBLICATION_CHUNK_BYTES = 8 * 1024 * 1024


@dataclass(slots=True)
class PublicationReceipt:
    attempt_id: str
    relative_path: str
    size: int
    sha256: str
    lease_generation: int
    effect_epoch: int = 0


PublicationCommit = Callable[[PublicationReceipt, Callable[[], None]], None]


class LocalPublisher:
    """Writes Local bytes through a fenced, descriptor-relative publication protocol."""

    def __init__(self, root: LocalRoot, leases: LeaseRegistry, supervisor_lock: SupervisorLock) -> None:
        self.root = root
        self.leases = leases
        self.supervisor_lock = supervisor_lock

    def publish(
        self,
        *,
        stream: io.BufferedIOBase | io.BytesIO,
        relative_path: str,
        attempt_id: str,
        expected_size: int,
        expected_sha256: str,
        lease: LeaseClaim,
        now: Callable[[], float],
        commit_receipt: PublicationCommit,
    ) -> PublicationReceipt:
        self.supervisor_lock.assert_held()
        if not attempt_id:
            raise ValueError("publication attempt ID is required")
        if expected_size < 0:
            raise ValueError("expected publication size cannot be negative")
        if not expected_sha256 or len(expected_sha256) != 64:
            raise ValueError("expected publication SHA-256 is required")

        temporary_path = f"{relative_path}.attempt-{uuid.uuid4().hex}.tmp"
        final_published = False
        try:
            self._write_and_verify_temporary(
                stream=stream,
                temporary_path=temporary_path,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                lease=lease,
                now=now,
            )
            self.leases.assert_active(lease, now=now())
            self.root.link_no_replace(temporary_path, relative_path)
            self.root.unlink(temporary_path, missing_ok=False)
            self.root.fsync_parent(relative_path)
            final_published = True
            self.root.verify_file(relative_path, expected_size=expected_size, expected_sha256=expected_sha256)
            self.leases.assert_active(lease, now=now())

            receipt = PublicationReceipt(
                attempt_id=attempt_id,
                relative_path=relative_path,
                size=expected_size,
                sha256=expected_sha256.lower(),
                lease_generation=lease.generation,
            )

            def set_effect_epoch() -> None:
                if receipt.effect_epoch:
                    raise LocalRootSafetyError("publication effect epoch was already set")
                receipt.effect_epoch = 1

            # The callback is the sole repository transaction boundary: it must
            # persist the receipt/location and invoke set_effect_epoch together.
            commit_receipt(receipt, set_effect_epoch)
            if receipt.effect_epoch != 1:
                raise LocalRootSafetyError("publication receipt was not committed with an effect epoch")
            self.leases.assert_active(lease, now=now())
            return receipt
        except BaseException:
            if not final_published:
                self.root.unlink(temporary_path)
            raise

    def _write_and_verify_temporary(
        self,
        *,
        stream: io.BufferedIOBase | io.BytesIO,
        temporary_path: str,
        expected_size: int,
        expected_sha256: str,
        lease: LeaseClaim,
        now: Callable[[], float],
    ) -> None:
        fd = self.root.create_exclusive(temporary_path)
        digest = hashlib.sha256()
        received = 0
        try:
            with os.fdopen(fd, "wb", closefd=False) as destination:
                while True:
                    self.supervisor_lock.assert_held()
                    self.leases.assert_active(lease, now=now())
                    chunk = stream.read(MAX_PUBLICATION_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("publication stream must return bytes")
                    received += len(chunk)
                    if received > expected_size:
                        raise LocalRootSafetyError("publication exceeds expected size")
                    destination.write(chunk)
                    digest.update(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if received != expected_size:
                raise LocalRootSafetyError("publication size does not match expectation")
            if digest.hexdigest().lower() != expected_sha256.lower():
                raise LocalRootSafetyError("publication SHA-256 does not match expectation")
        finally:
            os.close(fd)
        self.root.verify_file(temporary_path, expected_size=expected_size, expected_sha256=expected_sha256)
