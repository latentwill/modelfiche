from __future__ import annotations

import sqlite3

import pytest

from titles_api.storage.delivery_verifier import RemoteDeliveryEvidence, RemoteDeliveryVerificationError, RemoteDeliveryVerifier
from titles_api.storage.gates import GateEvidence, GateState, StorageGateStore


def _foundation(store: StorageGateStore):
    return store.verify_storage_contract(
        GateEvidence(
            run_lease_id="lease-1",
            alembic_head="0003",
            marker=1,
            binding_digest="binding",
            contract_version=1,
            evidence_digest="foundation-evidence",
        )
    )


def test_remote_delivery_verifier_requires_every_serving_proof_and_is_idempotent():
    store = StorageGateStore(sqlite3.connect(":memory:"))
    store.install_schema()
    foundation = _foundation(store)
    verifier = RemoteDeliveryVerifier(store)

    with pytest.raises(RemoteDeliveryVerificationError, match="conditional"):
        verifier.verify(
            foundation.verification_receipt_id or "",
            RemoteDeliveryEvidence(
                local_descriptor_serving=True,
                migrated_callers=True,
                exact_versioned_read=True,
                conditional_read=False,
            ),
        )

    evidence = RemoteDeliveryEvidence(
        local_descriptor_serving=True,
        migrated_callers=True,
        exact_versioned_read=True,
        conditional_read=True,
    )
    first = verifier.verify(foundation.verification_receipt_id or "", evidence)
    second = verifier.verify(foundation.verification_receipt_id or "", evidence)

    assert first.state is GateState.ready
    assert first.verification_receipt_id == second.verification_receipt_id
