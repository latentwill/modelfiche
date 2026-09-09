from __future__ import annotations

import sqlite3

import pytest

from titles_api.storage.gates import GateConflict, GateEvidence, GateState, StorageGateStore


def store() -> StorageGateStore:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    result = StorageGateStore(connection)
    result.install_schema()
    return result


def evidence(run_lease: str = "lease-1", digest: str = "schema-a") -> GateEvidence:
    return GateEvidence(
        run_lease_id=run_lease,
        alembic_head="0006_source_capability_timestamps",
        marker=1,
        binding_digest="binding-a",
        contract_version=1,
        evidence_digest=digest,
    )


def test_storage_contract_transition_is_receipted_and_idempotent() -> None:
    gates = store()
    first = gates.verify_storage_contract(evidence())
    second = gates.verify_storage_contract(evidence())

    assert first.state is GateState.ready
    assert second.verification_receipt_id == first.verification_receipt_id
    assert second.version == first.version


def test_storage_contract_rejects_stale_run_lease_without_overwriting_ready() -> None:
    gates = store()
    ready = gates.verify_storage_contract(evidence())

    with pytest.raises(GateConflict, match="run lease"):
        gates.verify_storage_contract(evidence(run_lease="lease-2"), expected_run_lease_id="lease-1")

    assert gates.get("storage_contract") == ready


def test_dependent_gate_requires_current_foundational_receipt() -> None:
    gates = store()
    foundation = gates.verify_storage_contract(evidence())

    dependent = gates.verify_feature("source_bound_io", foundation.verification_receipt_id, "source-proof-a")
    assert dependent.state is GateState.ready

    with pytest.raises(GateConflict, match="foundational receipt"):
        gates.verify_feature("remote_delivery", "stale", "delivery-proof")


def test_changed_evidence_blocks_ready_gate_until_explicit_retry() -> None:
    gates = store()
    foundation = gates.verify_storage_contract(evidence())
    gates.verify_feature("source_bound_io", foundation.verification_receipt_id, "source-proof-a")

    with pytest.raises(GateConflict, match="evidence changed"):
        gates.verify_feature("source_bound_io", foundation.verification_receipt_id, "source-proof-b")

    assert gates.get("source_bound_io").state is GateState.blocked
