from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import sqlite3
import uuid


FEATURE_GATES = (
    "storage_contract",
    "source_bound_io",
    "remote_delivery",
    "policy_import",
    "policy_fal",
    "storage_migration",
)


class GateState(StrEnum):
    disabled = "disabled"
    migrating = "migrating"
    ready = "ready"
    blocked = "blocked"


class GateConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GateEvidence:
    run_lease_id: str
    alembic_head: str
    marker: int
    binding_digest: str
    contract_version: int
    evidence_digest: str

    def canonical_payload(self) -> str:
        return json.dumps(
            {
                "alembic_head": self.alembic_head,
                "binding_digest": self.binding_digest,
                "contract_version": self.contract_version,
                "evidence_digest": self.evidence_digest,
                "marker": self.marker,
                "run_lease_id": self.run_lease_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class GateProjection:
    name: str
    state: GateState
    version: int
    verification_receipt_id: str | None
    blocked_reason: str | None


class StorageGateStore:
    """SQLite-backed gate control plane with immutable verification receipts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def install_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS storage_verification_receipts (
                id TEXT PRIMARY KEY,
                gate_name TEXT NOT NULL,
                parent_receipt_id TEXT,
                run_lease_id TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                evidence_payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(gate_name, parent_receipt_id, run_lease_id, evidence_digest)
            );
            CREATE TABLE IF NOT EXISTS storage_feature_gates (
                name TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK(state IN ('disabled','migrating','ready','blocked')),
                version INTEGER NOT NULL DEFAULT 0,
                verification_receipt_id TEXT,
                blocked_reason TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(verification_receipt_id) REFERENCES storage_verification_receipts(id)
            );
            """
        )
        now = _now()
        self.connection.executemany(
            "INSERT OR IGNORE INTO storage_feature_gates(name,state,version,updated_at) VALUES(?, 'disabled', 0, ?)",
            ((name, now) for name in FEATURE_GATES),
        )
        self.connection.commit()

    def get(self, name: str) -> GateProjection:
        self._validate_name(name)
        row = self.connection.execute(
            "SELECT name,state,version,verification_receipt_id,blocked_reason FROM storage_feature_gates WHERE name=?",
            (name,),
        ).fetchone()
        if row is None:
            raise KeyError(name)
        return _projection(row)

    def all(self) -> tuple[GateProjection, ...]:
        rows = self.connection.execute(
            "SELECT name,state,version,verification_receipt_id,blocked_reason FROM storage_feature_gates ORDER BY name"
        ).fetchall()
        return tuple(_projection(row) for row in rows)

    def verify_storage_contract(
        self,
        evidence: GateEvidence,
        *,
        expected_run_lease_id: str | None = None,
    ) -> GateProjection:
        if evidence.marker != 1:
            raise GateConflict("storage marker is not committed")
        if expected_run_lease_id is not None and evidence.run_lease_id != expected_run_lease_id:
            raise GateConflict("run lease changed")
        payload = evidence.canonical_payload()
        current = self.get("storage_contract")
        if current.state is GateState.ready and current.verification_receipt_id:
            receipt = self._receipt(current.verification_receipt_id)
            if receipt["evidence_payload"] == payload:
                return current
            self._block("storage_contract", "evidence_changed")
            raise GateConflict("storage-contract evidence changed")
        return self._record_ready(
            "storage_contract",
            parent_receipt_id=None,
            run_lease_id=evidence.run_lease_id,
            evidence_digest=evidence.evidence_digest,
            evidence_payload=payload,
        )

    def verify_feature(self, name: str, foundational_receipt_id: str, evidence_digest: str) -> GateProjection:
        self._validate_name(name)
        if name == "storage_contract":
            raise ValueError("use verify_storage_contract")
        foundation = self.get("storage_contract")
        if foundation.state is not GateState.ready or foundation.verification_receipt_id != foundational_receipt_id:
            raise GateConflict("foundational receipt is not current")
        current = self.get(name)
        if current.state is GateState.ready and current.verification_receipt_id:
            receipt = self._receipt(current.verification_receipt_id)
            if receipt["parent_receipt_id"] == foundational_receipt_id and receipt["evidence_digest"] == evidence_digest:
                return current
            self._block(name, "evidence_changed")
            raise GateConflict("feature evidence changed")
        foundation_receipt = self._receipt(foundational_receipt_id)
        return self._record_ready(
            name,
            parent_receipt_id=foundational_receipt_id,
            run_lease_id=foundation_receipt["run_lease_id"],
            evidence_digest=evidence_digest,
            evidence_payload=json.dumps({"evidence_digest": evidence_digest}, sort_keys=True, separators=(",", ":")),
        )

    def reset_all_disabled(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "UPDATE storage_feature_gates SET state='disabled',version=version+1,verification_receipt_id=NULL,blocked_reason=NULL,updated_at=?",
                (_now(),),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _record_ready(
        self,
        name: str,
        *,
        parent_receipt_id: str | None,
        run_lease_id: str,
        evidence_digest: str,
        evidence_payload: str,
    ) -> GateProjection:
        receipt_id = str(uuid.uuid4())
        now = _now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT id FROM storage_verification_receipts WHERE gate_name=? AND parent_receipt_id IS ? AND run_lease_id=? AND evidence_digest=?",
                (name, parent_receipt_id, run_lease_id, evidence_digest),
            ).fetchone()
            if existing is not None:
                receipt_id = str(existing["id"])
            else:
                self.connection.execute(
                    "INSERT INTO storage_verification_receipts(id,gate_name,parent_receipt_id,run_lease_id,evidence_digest,evidence_payload,created_at) VALUES(?,?,?,?,?,?,?)",
                    (receipt_id, name, parent_receipt_id, run_lease_id, evidence_digest, evidence_payload, now),
                )
            changed = self.connection.execute(
                "UPDATE storage_feature_gates SET state='ready',version=version+1,verification_receipt_id=?,blocked_reason=NULL,updated_at=? WHERE name=? AND state IN ('disabled','migrating','blocked')",
                (receipt_id, now, name),
            ).rowcount
            if changed != 1:
                raise GateConflict(f"{name} gate changed concurrently")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return self.get(name)

    def _block(self, name: str, reason: str) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "UPDATE storage_feature_gates SET state='blocked',version=version+1,verification_receipt_id=NULL,blocked_reason=?,updated_at=? WHERE name=?",
                (reason, _now(), name),
            )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _receipt(self, receipt_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM storage_verification_receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise GateConflict("verification receipt not found")
        return row

    @staticmethod
    def _validate_name(name: str) -> None:
        if name not in FEATURE_GATES:
            raise KeyError(name)


def _projection(row: sqlite3.Row) -> GateProjection:
    return GateProjection(
        name=str(row["name"]),
        state=GateState(str(row["state"])),
        version=int(row["version"]),
        verification_receipt_id=row["verification_receipt_id"],
        blocked_reason=row["blocked_reason"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
