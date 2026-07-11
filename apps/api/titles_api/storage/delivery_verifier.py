from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from .gates import GateProjection, StorageGateStore


class RemoteDeliveryVerificationError(RuntimeError):
    """Installed-data evidence is insufficient to enable remote delivery."""


@dataclass(frozen=True, slots=True)
class RemoteDeliveryEvidence:
    local_descriptor_serving: bool
    migrated_callers: bool
    exact_versioned_read: bool
    conditional_read: bool

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def assert_complete(self) -> None:
        missing = [
            label
            for label, available in (
                ("local descriptor serving", self.local_descriptor_serving),
                ("migrated callers", self.migrated_callers),
                ("exact versioned read", self.exact_versioned_read),
                ("conditional read", self.conditional_read),
            )
            if not available
        ]
        if missing:
            raise RemoteDeliveryVerificationError(f"remote delivery verification is missing {', '.join(missing)}")


class RemoteDeliveryVerifier:
    """The sole readiness transition for repository-owned remote delivery."""

    def __init__(self, gates: StorageGateStore) -> None:
        self._gates = gates

    def verify(self, foundational_receipt_id: str, evidence: RemoteDeliveryEvidence) -> GateProjection:
        if not foundational_receipt_id:
            raise RemoteDeliveryVerificationError("remote delivery requires a current foundational receipt")
        evidence.assert_complete()
        return self._gates.verify_feature("remote_delivery", foundational_receipt_id, evidence.digest())
