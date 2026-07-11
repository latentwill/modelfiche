from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .backup import BackupReceipt, EnvelopeStore
from .database_set import DatabaseSetRestorer


SANITIZED_CONFIRMATION = "ROLL BACK TO CUTOVER SNAPSHOT"


class RestoreForbidden(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RestoreEligibility:
    gate_states: Mapping[str, str]
    effect_epoch: int
    effect_rows: int


def assert_sanitized_restore_allowed(
    gate_states: Mapping[str, str],
    *,
    effect_epoch: int,
    effect_rows: int,
) -> None:
    if any(state != "disabled" for state in gate_states.values()) or effect_epoch != 0 or effect_rows != 0:
        raise RestoreForbidden("sanitized rollback is forbidden after enable or external effect")


def restore_sanitized(
    *,
    envelope_store: EnvelopeStore,
    backup_id: str,
    database_path: Path,
    eligibility: RestoreEligibility,
    confirmation: str,
) -> BackupReceipt:
    if confirmation != SANITIZED_CONFIRMATION:
        raise RestoreForbidden("exact sanitized rollback confirmation is required")
    assert_sanitized_restore_allowed(
        eligibility.gate_states,
        effect_epoch=eligibility.effect_epoch,
        effect_rows=eligibility.effect_rows,
    )
    staged = database_path.with_name(f".{database_path.name}.{backup_id}.decrypted")
    try:
        receipt = envelope_store.restore(backup_id, staged)
        if receipt.kind != "sanitized":
            raise RestoreForbidden("requested backup is not sanitized")
        DatabaseSetRestorer(database_path).replace(staged)
        return receipt
    finally:
        staged.unlink(missing_ok=True)


def restore_raw(
    *,
    envelope_store: EnvelopeStore,
    backup_id: str,
    database_path: Path,
) -> BackupReceipt:
    staged = database_path.with_name(f".{database_path.name}.{backup_id}.raw")
    try:
        receipt = envelope_store.restore(backup_id, staged)
        if receipt.kind != "raw":
            raise RestoreForbidden("requested backup is not raw")
        DatabaseSetRestorer(database_path).replace(staged)
        return receipt
    finally:
        staged.unlink(missing_ok=True)
