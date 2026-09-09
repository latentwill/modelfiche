from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services import current_workspace, record_activity
from .contracts import (
    AnnouncementV1,
    StorageDestinationV1,
    StorageMigrationActionRequestV1,
    StorageMigrationActionResultV1,
    StorageMigrationAdmitRequestV1,
    StorageMigrationCountsV1,
    StorageMigrationDetailV1,
    StorageMigrationHistoryV1,
    StorageMigrationPreviewReasonV1,
    StorageMigrationPreviewRequestV1,
    StorageMigrationPreviewV1,
    StorageMigrationScopeV1,
    parse_migration_action,
)
from .gates import GateState
from .policy import list_policy_candidates, project_policy, policy_snapshot
from .repository import MigrationPreviewConflict, PolicyConflict, StorageNotFound, StorageRepository, StorageRepositoryError


migration_router = APIRouter(tags=["storage-migrations"])
DB = Depends(get_db)

_TERMINAL_STATES = {
    "completed",
    "failed",
    "abandoned",
    "superseded_destination",
    "superseded_source_changed",
}
_ACTIVE_STATES = {
    "queued",
    "running",
    "cancel_requested",
    "partial",
    "retryable",
    "capacity_blocked",
    "canceled_with_remaining",
}
_TERMINAL_OUTCOMES = {"committed", "copied", "reused", "failed", "source_changed", "canceled"}
_COMMITTED_OUTCOMES = {"committed", "copied", "reused"}
_ACTION_TO_EVENT = {
    "cancel": "cancel_requested",
    "resume_remaining": "continued",
    "retry_failed": "continued",
    "continue_incomplete": "continued",
    "review_changed": "continued",
    "replace_destination": "superseded",
    "abandon_remaining": "abandoned",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)
def _expired(value: datetime) -> bool:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized <= _now()



def _json_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256(payload).hexdigest()


def _scope_values(scope: StorageMigrationScopeV1) -> tuple[str, str | None, dict[str, Any]]:
    data = scope.model_dump(mode="json")
    return str(data["kind"]), data.get("project_id"), data


def _destination_values(destination: StorageDestinationV1) -> tuple[str, str | None, str | None, dict[str, Any]]:
    data = destination.model_dump(mode="json")
    if data["kind"] == "local":
        return "local", None, str(data["root_id"]), data
    return "s3", str(data["source_id"]), None, data


def _gate_ready(db: Session) -> tuple[bool, str | None]:
    try:
        contract_row = db.execute(
            text("SELECT state, verification_receipt_id FROM storage_feature_gates WHERE name='storage_contract'")
        ).mappings().one_or_none()
        migration_row = db.execute(
            text("SELECT state, verification_receipt_id FROM storage_feature_gates WHERE name='storage_migration'")
        ).mappings().one_or_none()
        if contract_row is None or migration_row is None:
            return False, "storage_contract_missing"
        if contract_row["state"] != GateState.ready.value:
            return False, "storage_contract_not_ready"
        if migration_row["state"] != GateState.ready.value:
            return False, "storage_migration_not_ready"
        parent = db.execute(
            text("SELECT parent_receipt_id FROM storage_verification_receipts WHERE id=:id"),
            {"id": migration_row["verification_receipt_id"]},
        ).scalar_one_or_none()
        if parent != contract_row["verification_receipt_id"]:
            return False, "storage_contract_receipt_stale"
    except Exception:
        return False, "storage_contract_missing"
    return True, None
def _begin_immediate(db: Session) -> None:
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        db.commit()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")



def _require_gate(db: Session) -> None:
    ready, reason = _gate_ready(db)
    if not ready:
        state = db.execute(text("SELECT state FROM storage_feature_gates WHERE name='storage_migration'")).scalar_one_or_none()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "storage_feature_not_ready",
                "gate": "storage_migration",
                "state": state or "disabled",
                "redacted_reason": reason,
            },
        )


def _destination_ready(db: Session, workspace_id: str, destination: StorageDestinationV1) -> tuple[bool, str | None]:
    kind, source_id, root_id, _ = _destination_values(destination)
    if kind == "local":
        root = db.scalar(select(models.LocalRoot).where(models.LocalRoot.id == root_id, models.LocalRoot.workspace_id == workspace_id))
        if root is None:
            return False, "destination_not_ready"
        if root.availability != "available" or root.retired_at is not None:
            return False, "destination_not_ready"
        return True, None
    source = db.scalar(select(models.ImportSource).where(models.ImportSource.id == source_id, models.ImportSource.workspace_id == workspace_id))
    if source is None or not source.is_active:
        return False, "destination_not_ready"
    candidate = next((item for item in list_policy_candidates(db, workspace_id) if str(item.source_id) == source.id), None)
    if candidate is None or candidate.state != "write_ready":
        return False, "destination_not_ready"
    return True, None


def _scope_assets(db: Session, workspace_id: str, scope_kind: str, project_id: str | None) -> list[models.Asset]:
    query = select(models.Asset).where(
        models.Asset.workspace_id == workspace_id,
        models.Asset.kind == models.AssetKind.image,
    )
    if scope_kind == "project":
        query = query.where(models.Asset.project_id == project_id)
    elif scope_kind == "unassigned":
        query = query.where(models.Asset.project_id.is_(None))
    return list(db.scalars(query.order_by(models.Asset.created_at, models.Asset.id)))


def _select_source_location(db: Session, asset: models.Asset) -> models.AssetLocation | None:
    locations = list(
        db.scalars(
            select(models.AssetLocation).where(
                models.AssetLocation.workspace_id == asset.workspace_id,
                models.AssetLocation.asset_id == asset.id,
                models.AssetLocation.verification_state == "available",
            )
        )
    )
    locations.sort(
        key=lambda row: (
            0 if row.id == asset.preferred_location_id else 1,
            -(row.last_verified_at.timestamp() if row.last_verified_at else 0),
            row.id,
        )
    )
    return locations[0] if locations else None


def _preview_items(db: Session, workspace_id: str, scope_kind: str, project_id: str | None, destination: StorageDestinationV1) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    destination_kind, destination_source_id, destination_root_id, destination_data = _destination_values(destination)
    items: list[dict[str, Any]] = []
    source_identity: list[str] = []
    ordered_assets: list[str] = []
    for ordinal, asset in enumerate(_scope_assets(db, workspace_id, scope_kind, project_id)):
        ordered_assets.append(asset.id)
        location = _select_source_location(db, asset)
        known_size: int | None = None
        if location is not None:
            known_size = location.verified_size if location.verified_size is not None else location.size
            source_identity.append(location.source_revision_fingerprint or location.id)
        eligible = location is not None
        reason = None if eligible else "source_unavailable"
        if eligible and location is not None:
            if destination_kind == "local" and location.provider == "local" and location.local_root_id == destination_root_id:
                eligible = False
                reason = "destination_not_ready"
            elif destination_kind == "s3" and location.provider == "s3" and location.source_id == destination_source_id:
                eligible = False
                reason = "destination_not_ready"
        items.append(
            {
                "asset_id": asset.id,
                "source_location_id": location.id if location is not None else None,
                "content_blob_id": asset.content_blob_id,
                "selected_revision_fingerprint": location.source_revision_fingerprint if location is not None else None,
                "eligible": eligible,
                "reason_code": reason,
                "known_size": known_size,
            }
        )
    counts = {
        "source_inventory_digest": _json_digest(sorted(source_identity)),
        "ordered_asset_digest": _json_digest(ordered_assets),
        "source_snapshot_ids": sorted({value for value in source_identity if len(value) == 36}),
        "destination": destination_data,
    }
    return items, counts


def _reason_rows(items: list[dict[str, Any]], *, destination_ready: bool, capacity_blocked: bool) -> list[StorageMigrationPreviewReasonV1]:
    reasons: list[StorageMigrationPreviewReasonV1] = []
    if not destination_ready:
        reasons.append(StorageMigrationPreviewReasonV1(code="destination_not_ready", redacted_label="Destination is not ready.", affected_items=len(items)))
    missing = sum(1 for item in items if item.get("reason_code") == "source_unavailable")
    if missing:
        reasons.append(StorageMigrationPreviewReasonV1(code="source_unavailable", redacted_label="Source bytes are unavailable.", affected_items=missing))
    unknown = sum(1 for item in items if item.get("eligible") and item.get("known_size") is None)
    if unknown and capacity_blocked:
        reasons.append(StorageMigrationPreviewReasonV1(code="capacity", redacted_label="Capacity cannot be reserved for unknown-size items.", affected_items=unknown))
    eligible = sum(1 for item in items if item.get("eligible"))
    if eligible == 0:
        reasons.append(StorageMigrationPreviewReasonV1(code="no_eligible_images", redacted_label="No eligible images are in this scope.", affected_items=len(items)))
    return reasons


def _preview_dto(preview: models.StorageMigrationPreview) -> StorageMigrationPreviewV1:
    frozen = dict(preview.frozen_fields or {})
    scope = frozen.get("scope") or {"kind": preview.scope_kind, **({"project_id": preview.project_id} if preview.project_id else {})}
    destination = frozen.get("destination") or ({"kind": "local", "root_id": preview.destination_root_id} if preview.destination_provider == "local" else {"kind": "s3", "source_id": preview.destination_source_id})
    counts = StorageMigrationCountsV1(
        eligible_items=preview.eligible_count,
        excluded_items=preview.excluded_count,
        terminal_items=0,
        committed_items=0,
        failed_items=0,
        remaining_items=preview.eligible_count,
        unknown_size_items=preview.unknown_bytes,
        known_bytes_total=preview.known_bytes,
        committed_bytes=0,
        inflight_bytes=0,
        worst_case_bytes=preview.worst_case_bytes,
    )
    reason_codes: list[str] = []
    if preview.disabled_reason:
        reason_codes.append(preview.disabled_reason)
    missing = int(frozen.get("source_unavailable_items", 0))
    if missing:
        reason_codes.append("source_unavailable")
    if preview.eligible_count == 0:
        reason_codes.append("no_eligible_images")
    if not reason_codes and preview.readiness_state != "ready":
        reason_codes.append("destination_not_ready")
    reasons = [
        StorageMigrationPreviewReasonV1(
            code=code if code in {"no_eligible_images", "destination_not_ready", "capacity", "source_unavailable", "replacement_conflict"} else "destination_not_ready",
            redacted_label={
                "no_eligible_images": "No eligible images are in this scope.",
                "destination_not_ready": "Destination is not ready.",
                "capacity": "Capacity cannot be reserved.",
                "source_unavailable": "Source bytes are unavailable.",
                "replacement_conflict": "The predecessor changed; review the replacement.",
            }[code if code in {"no_eligible_images", "destination_not_ready", "capacity", "source_unavailable", "replacement_conflict"} else "destination_not_ready"],
            affected_items=int(frozen.get("unknown_size_items", preview.unknown_bytes)) if code == "capacity" else (preview.excluded_count if code != "no_eligible_images" else preview.excluded_count + preview.eligible_count),
        )
        for code in dict.fromkeys(reason_codes)
    ]
    frozen_public = {key: value for key, value in frozen.items() if key in {"workspace_id", "scope", "destination", "source_snapshot_ids", "source_inventory_digest", "ordered_asset_digest", "policy_version", "created_at"}}
    from .contracts import LocalStorageDestinationV1, S3StorageDestinationV1, StorageMigrationFrozenFieldsV1, StorageMigrationScopeAllV1, StorageMigrationScopeProjectV1, StorageMigrationScopeUnassignedV1
    scope_model = StorageMigrationScopeProjectV1.model_validate(scope) if scope.get("kind") == "project" else StorageMigrationScopeUnassignedV1.model_validate(scope) if scope.get("kind") == "unassigned" else StorageMigrationScopeAllV1.model_validate(scope)
    dest_model = LocalStorageDestinationV1.model_validate(destination) if destination.get("kind") == "local" else S3StorageDestinationV1.model_validate(destination)
    frozen_model = StorageMigrationFrozenFieldsV1(
        workspace_id=preview.workspace_id,
        scope=scope_model,
        destination=dest_model,
        source_snapshot_ids=frozen.get("source_snapshot_ids", []),
        source_inventory_digest=frozen.get("source_inventory_digest", "empty"),
        ordered_asset_digest=frozen.get("ordered_asset_digest", "empty"),
        policy_version=int(frozen.get("policy_version", 1)),
        created_at=frozen.get("created_at", preview.created_at),
    )
    return StorageMigrationPreviewV1(
        id=preview.id,
        version=preview.version,
        expires_at=preview.expires_at,
        frozen_fields=frozen_model,
        counts=counts,
        readiness="ready" if preview.readiness_state == "ready" else "blocked",
        reasons=reasons,
        admit_enabled=preview.readiness_state == "ready" and preview.eligible_count > 0,
        disabled_reason_code=preview.disabled_reason,
        replaces_parent_id=preview.predecessor_id,
        expected_parent_version=preview.predecessor_version,
        expected_owner_epoch=preview.expected_owner_epoch,
        focus_target_id="migration_preview_status",
    )


def create_preview(db: Session, workspace_id: str, request: StorageMigrationPreviewRequestV1) -> StorageMigrationPreviewV1:
    scope_kind, project_id, scope_data = _scope_values(request.scope)
    destination_kind, destination_source_id, destination_root_id, destination_data = _destination_values(request.destination)
    if scope_kind == "project" and db.scalar(select(models.Project).where(models.Project.id == project_id, models.Project.workspace_id == workspace_id)) is None:
        raise StorageNotFound("project")
    if request.replacement_parent_id is not None:
        predecessor = db.scalar(select(models.StorageMigration).where(models.StorageMigration.id == str(request.replacement_parent_id), models.StorageMigration.workspace_id == workspace_id))
        if predecessor is None:
            raise StorageNotFound("predecessor migration")
        if request.expected_parent_version is None or predecessor.version != request.expected_parent_version:
            raise MigrationPreviewConflict("predecessor migration version is stale")
        if predecessor.state in _TERMINAL_STATES:
            raise MigrationPreviewConflict("predecessor migration is terminal")
        owner = db.scalar(select(models.MigrationExecutionOwner).where(models.MigrationExecutionOwner.workspace_id == workspace_id, models.MigrationExecutionOwner.parent_id == predecessor.id))
        if owner is None:
            raise MigrationPreviewConflict("predecessor migration owner is stale")
        expected_owner_epoch = owner.owner_epoch
    else:
        expected_owner_epoch = None
    destination_ready, destination_reason = _destination_ready(db, workspace_id, request.destination)
    items, identities = _preview_items(db, workspace_id, scope_kind, project_id, request.destination)
    source_unavailable = sum(1 for item in items if item.get("reason_code") == "source_unavailable")
    unknown = sum(1 for item in items if item.get("eligible") and item.get("known_size") is None)
    capacity_blocked = unknown > 0
    reasons = _reason_rows(items, destination_ready=destination_ready, capacity_blocked=capacity_blocked)
    eligible = sum(1 for item in items if item.get("eligible"))
    known_sizes = [int(item["known_size"]) for item in items if item.get("eligible") and item.get("known_size") is not None]
    policy = project_policy(db, workspace_id)
    frozen = {
        "workspace_id": workspace_id,
        "scope": scope_data,
        "destination": destination_data,
        "source_snapshot_ids": identities["source_snapshot_ids"],
        "source_inventory_digest": identities["source_inventory_digest"],
        "ordered_asset_digest": identities["ordered_asset_digest"],
        "policy_version": policy.version,
        "source_unavailable_items": source_unavailable,
        "unknown_size_items": unknown,
        "created_at": _now().isoformat(),
    }
    readiness = "ready" if destination_ready and eligible > 0 and not capacity_blocked else "blocked"
    disabled_reason = destination_reason or ("capacity" if capacity_blocked else ("no_eligible_images" if eligible == 0 else None))
    repository = StorageRepository(db, workspace_id)
    preview = repository.create_migration_preview(
        scope_kind=scope_kind,
        project_id=project_id,
        destination_provider=destination_kind,
        destination_root_id=destination_root_id,
        destination_source_id=destination_source_id,
        expires_in=timedelta(minutes=15),
        items=items,
        predecessor_id=str(request.replacement_parent_id) if request.replacement_parent_id else None,
        predecessor_version=request.expected_parent_version,
        expected_owner_epoch=expected_owner_epoch,
        frozen_fields=frozen,
        readiness_state=readiness,
        disabled_reason=disabled_reason,
    )
    db.commit()
    return _preview_dto(preview)


def _active_migration(db: Session, workspace_id: str) -> models.StorageMigration | None:
    return db.scalar(
        select(models.StorageMigration)
        .where(models.StorageMigration.workspace_id == workspace_id, models.StorageMigration.state.in_(_ACTIVE_STATES))
        .order_by(models.StorageMigration.created_at, models.StorageMigration.id)
    )


def _snapshot_for_migration(db: Session, workspace_id: str, preview: models.StorageMigrationPreview, *, operation_id: str) -> models.StoragePolicySnapshot:
    projection = project_policy(db, workspace_id)
    source = db.get(models.ImportSource, preview.destination_source_id) if preview.destination_source_id else None
    snapshot = policy_snapshot(db, workspace_id, operation_id=operation_id)
    snapshot.resolved_provider = preview.destination_provider
    if preview.destination_provider == "s3":
        snapshot.write_source_id = preview.destination_source_id
        snapshot.write_source_fingerprint = source.identity_fingerprint if source else None
        snapshot.write_managed_prefix = source.managed_prefix if source else None
    else:
        snapshot.local_root_id = preview.destination_root_id
    snapshot.desired_provider = projection.desired_provider
    snapshot.fal_local_fallback = projection.fal_local_fallback
    db.flush()
    return snapshot


def _admit(db: Session, workspace_id: str, preview_id: str, expected_version: int, *, predecessor: models.StorageMigration | None = None) -> models.StorageMigration:
    preview = db.scalar(select(models.StorageMigrationPreview).where(models.StorageMigrationPreview.id == preview_id, models.StorageMigrationPreview.workspace_id == workspace_id))
    if preview is None:
        raise StorageNotFound("storage migration preview")
    if preview.version != expected_version or preview.state != "open" or _expired(preview.expires_at):
        raise MigrationPreviewConflict("migration preview is stale or expired")
    if preview.readiness_state != "ready" or preview.eligible_count == 0:
        raise MigrationPreviewConflict("migration preview is blocked")
    if predecessor is None:
        active = _active_migration(db, workspace_id)
        if active is not None:
            raise PolicyConflict(f"storage migration is already active: {active.id}")
    preview.state = "consumed"
    preview.version += 1
    snapshot = _snapshot_for_migration(db, workspace_id, preview, operation_id=f"migration:{preview.id}")
    migration = models.StorageMigration(
        workspace_id=workspace_id,
        snapshot_id=snapshot.id,
        version=0,
        state="queued",
        scope_kind=preview.scope_kind,
        project_id=preview.project_id,
        destination_provider=preview.destination_provider,
        destination_source_id=preview.destination_source_id,
        destination_root_id=preview.destination_root_id,
        predecessor_id=predecessor.id if predecessor is not None else preview.predecessor_id,
    )
    db.add(migration)
    db.flush()
    items = list(db.scalars(select(models.StorageMigrationPreviewItem).where(models.StorageMigrationPreviewItem.preview_id == preview.id).order_by(models.StorageMigrationPreviewItem.ordinal, models.StorageMigrationPreviewItem.id)))
    for item in items:
        if item.source_location_id is None:
            continue
        db.add(models.StorageMigrationItem(
            migration_id=migration.id,
            ordinal=item.ordinal,
            asset_id=item.asset_id,
            content_blob_id=item.content_blob_id,
            source_location_id=item.source_location_id,
            destination_provider=item.destination_provider,
            destination_source_id=item.destination_source_id,
            destination_root_id=item.destination_root_id,
            eligible=item.eligible,
            reason_code=item.reason_code,
            outcome=None,
            generation=0,
        ))
    db.flush()
    owner = db.scalar(select(models.MigrationExecutionOwner).where(models.MigrationExecutionOwner.workspace_id == workspace_id))
    if owner is None:
        db.add(models.MigrationExecutionOwner(workspace_id=workspace_id, parent_id=migration.id, owner_epoch=1))
    else:
        owner.parent_id = migration.id
        owner.owner_epoch += 1
    db.flush()
    counts = {
        "eligible_items": preview.eligible_count,
        "excluded_items": preview.excluded_count,
        "unknown_size_items": preview.unknown_bytes,
        "known_bytes_total": preview.known_bytes,
        "worst_case_bytes": preview.worst_case_bytes,
    }
    record_activity(db, workspace_id=workspace_id, action="admitted", subject_type="storage_migration", subject_id=migration.id, profile_id=None, details={"counts": counts, "frozen_fields": preview.frozen_fields})
    db.commit()
    return migration


def _admission_snapshot(db: Session, migration: models.StorageMigration) -> dict[str, Any]:
    event = db.scalar(select(models.ActivityEvent).where(models.ActivityEvent.workspace_id == migration.workspace_id, models.ActivityEvent.subject_type == "storage_migration", models.ActivityEvent.subject_id == migration.id, models.ActivityEvent.action == "admitted").order_by(models.ActivityEvent.created_at, models.ActivityEvent.id))
    return dict(event.details or {}) if event else {}


def _detail(db: Session, migration: models.StorageMigration, *, focus_target_id: str = "migration_status") -> StorageMigrationDetailV1:
    snapshot_data = _admission_snapshot(db, migration)
    frozen = dict(snapshot_data.get("frozen_fields") or {})
    if not frozen:
        frozen = {"workspace_id": migration.workspace_id, "scope": {"kind": migration.scope_kind, **({"project_id": migration.project_id} if migration.project_id else {})}, "destination": ({"kind": "local", "root_id": migration.destination_root_id} if migration.destination_provider == "local" else {"kind": "s3", "source_id": migration.destination_source_id}), "source_snapshot_ids": [], "source_inventory_digest": "unknown", "ordered_asset_digest": "unknown", "policy_version": 1, "created_at": migration.created_at}
    items = list(db.scalars(select(models.StorageMigrationItem).where(models.StorageMigrationItem.migration_id == migration.id).order_by(models.StorageMigrationItem.ordinal, models.StorageMigrationItem.id)))
    eligible = int((snapshot_data.get("counts") or {}).get("eligible_items", sum(1 for item in items if item.eligible)))
    excluded = int((snapshot_data.get("counts") or {}).get("excluded_items", 0))
    terminal = [item for item in items if item.outcome in _TERMINAL_OUTCOMES]
    committed = [item for item in terminal if item.outcome in _COMMITTED_OUTCOMES]
    failed = [item for item in terminal if item.outcome in {"failed", "source_changed"}]
    unknown_count = int((snapshot_data.get("counts") or {}).get("unknown_size_items", 0))
    known_bytes = int((snapshot_data.get("counts") or {}).get("known_bytes_total", 0))
    committed_bytes = 0
    for item in committed:
        location = db.get(models.AssetLocation, item.source_location_id)
        if location is not None:
            committed_bytes += int(location.verified_size if location.verified_size is not None else location.size or 0)
    counts = StorageMigrationCountsV1(
        eligible_items=eligible,
        excluded_items=excluded,
        terminal_items=len(terminal),
        committed_items=len(committed),
        failed_items=len(failed),
        remaining_items=max(0, eligible - len(terminal)),
        unknown_size_items=unknown_count,
        known_bytes_total=known_bytes,
        committed_bytes=committed_bytes,
        inflight_bytes=0,
        worst_case_bytes=max(known_bytes, committed_bytes),
    )
    events = list(db.scalars(select(models.ActivityEvent).where(models.ActivityEvent.workspace_id == migration.workspace_id, models.ActivityEvent.subject_type == "storage_migration", models.ActivityEvent.subject_id == migration.id).order_by(models.ActivityEvent.created_at, models.ActivityEvent.id)))
    history: list[StorageMigrationHistoryV1] = []
    for sequence, event in enumerate(events):
        code = event.action if event.action in {"admitted", "started", "item_committed", "item_failed", "cancel_requested", "capacity_blocked", "continued", "abandoned", "superseded", "completed"} else "continued"
        history.append(StorageMigrationHistoryV1(sequence=sequence, at=event.created_at, event_code=code, redacted_summary=str((event.details or {}).get("message") or event.action)))
    remaining = counts.remaining_items > 0
    terminal_state = migration.state not in _ACTIVE_STATES
    controls = []
    for action, label in (("cancel", "Cancel"), ("resume_remaining", "Resume remaining"), ("retry_failed", "Retry failed"), ("continue_incomplete", "Continue incomplete"), ("review_changed", "Review changed items"), ("replace_destination", "Replace destination"), ("abandon_remaining", "Abandon remaining")):
        enabled = not terminal_state
        if action == "cancel":
            enabled = enabled and remaining
        elif action in {"resume_remaining", "continue_incomplete"}:
            enabled = enabled and remaining and migration.state in {"partial", "retryable", "capacity_blocked", "canceled_with_remaining"}
        elif action == "retry_failed":
            enabled = enabled and bool(failed)
        elif action == "review_changed":
            enabled = enabled and bool(failed)
        elif action == "replace_destination":
            enabled = enabled and remaining
        elif action == "abandon_remaining":
            enabled = enabled and remaining
        controls.append({"action": action, "label": label, "enabled": enabled, "disabled_reason_code": None if enabled else ("operator_canceled" if migration.state == "cancel_requested" else "transfer_failed" if action == "retry_failed" and not failed else None)})
    from .contracts import StorageMigrationDetailV1
    replacement_links = []
    if migration.predecessor_id:
        replacement_links.append({"relation": "predecessor", "migration_id": migration.predecessor_id, "route": f"#/transfers/migrations/{migration.predecessor_id}"})
    successors = list(db.scalars(select(models.StorageMigration).where(models.StorageMigration.predecessor_id == migration.id).order_by(models.StorageMigration.created_at, models.StorageMigration.id)))
    replacement_links.extend({"relation": "successor", "migration_id": successor.id, "route": f"#/transfers/migrations/{successor.id}"} for successor in successors)
    frozen_public = {key: value for key, value in frozen.items() if key in {"workspace_id", "scope", "destination", "source_snapshot_ids", "source_inventory_digest", "ordered_asset_digest", "policy_version", "created_at"}}
    return StorageMigrationDetailV1(
        id=migration.id,
        version=migration.version,
        canonical_route=f"#/transfers/migrations/{migration.id}",
        state=migration.state,
        reason_code=migration.reason_code,
        frozen_fields=frozen_public,
        counts=counts,
        progress={"value": counts.terminal_items, "max": counts.eligible_items},
        history=history,
        controls=controls,
        replacement_links=replacement_links,
        focus_target_id=focus_target_id,
    )


def _action_conflict(db: Session, migration: models.StorageMigration, message: str) -> HTTPException:
    detail = _detail(db, migration, focus_target_id="migration_action_error")
    return HTTPException(status_code=409, detail={"code": "storage_migration_conflict", "message": message, "current": detail.model_dump(mode="json"), "winner_route": detail.canonical_route, "focus_target_id": "migration_action_error"})


def _announcement(message: str, *, mode: str = "assertive", focus: str | None = "migration_status") -> AnnouncementV1:
    return AnnouncementV1(event_id=str(uuid4()), mode=mode, message=message, focus_target_id=focus)


def admit_preview(db: Session, workspace_id: str, request: StorageMigrationAdmitRequestV1) -> dict[str, Any]:
    try:
        migration = _admit(db, workspace_id, str(request.preview_id), request.expected_preview_version)
    except PolicyConflict as exc:
        active = _active_migration(db, workspace_id)
        if active is None:
            raise
        current = _detail(db, active)
        return {"kind": "conflict", "current": current.model_dump(mode="json"), "winner_route": current.canonical_route, "focus_target_id": "migration_admit_error", "announcement": _announcement("Another migration is already active.").model_dump(mode="json")}
    return {"kind": "accepted", "detail": _detail(db, migration).model_dump(mode="json"), "announcement": _announcement("Storage migration admitted.").model_dump(mode="json")}


def perform_action(db: Session, workspace_id: str, migration_id: str, raw_action: Any) -> dict[str, Any]:
    action = parse_migration_action(raw_action)
    migration = db.scalar(select(models.StorageMigration).where(models.StorageMigration.id == migration_id, models.StorageMigration.workspace_id == workspace_id))
    if migration is None:
        raise StorageNotFound("storage migration")
    if migration.version != action.expected_version:
        raise _action_conflict(db, migration, "storage migration version is stale")
    if migration.state in _TERMINAL_STATES and action.action != "review_changed":
        raise _action_conflict(db, migration, "storage migration is terminal")
    controls = {control.action: control for control in _detail(db, migration).controls}
    selected_control = controls.get(action.action)
    if selected_control is not None and not selected_control.enabled:
        raise _action_conflict(db, migration, f"migration action {action.action} is disabled")
    if action.action == "review_changed":
        request = StorageMigrationPreviewRequestV1.model_validate({"scope": {"kind": migration.scope_kind, **({"project_id": migration.project_id} if migration.project_id else {})}, "destination": ({"kind": "local", "root_id": migration.destination_root_id} if migration.destination_provider == "local" else {"kind": "s3", "source_id": migration.destination_source_id}), "replacement_parent_id": migration.id, "expected_parent_version": migration.version})
        preview = create_preview(db, workspace_id, request)
        record_activity(db, workspace_id=workspace_id, action="continued", subject_type="storage_migration", subject_id=migration.id, profile_id=None, details={"message": "Review changed items and confirm the new preview."})
        db.commit()
        return {"kind": "preview", "preview": preview.model_dump(mode="json"), "destination_route": "#/transfers", "focus_target_id": "start-migration", "announcement": _announcement("Source or scope changed; review the new migration preview.").model_dump(mode="json")}
    if action.action == "replace_destination":
        preview = db.scalar(select(models.StorageMigrationPreview).where(models.StorageMigrationPreview.id == str(action.preview_id), models.StorageMigrationPreview.workspace_id == workspace_id))
        if preview is None or preview.predecessor_id != migration.id:
            raise _action_conflict(db, migration, "replacement preview does not belong to this migration")
        successor = _admit(db, workspace_id, preview.id, action.preview_version, predecessor=migration)
        migration.state = "superseded_destination"
        migration.reason_code = "destination_unavailable"
        migration.version += 1
        for transfer in list(db.scalars(select(models.StorageTransfer).where(models.StorageTransfer.workspace_id == workspace_id, models.StorageTransfer.migration_id == migration.id))):
            try:
                StorageRepository(db, workspace_id).mark_transfer_reclaim_pending(transfer.id)
            except StorageRepositoryError:
                pass
        record_activity(db, workspace_id=workspace_id, action="superseded", subject_type="storage_migration", subject_id=migration.id, profile_id=None, details={"message": "Migration superseded by a destination replacement."})
        db.commit()
        return {"kind": "detail", "detail": _detail(db, successor).model_dump(mode="json"), "announcement": _announcement("Destination replaced; successor migration admitted.").model_dump(mode="json")}
    if action.action == "cancel":
        remaining = _detail(db, migration).counts.remaining_items
        migration.state = "canceled_with_remaining" if remaining else "completed"
        migration.reason_code = "operator_canceled"
        if not remaining:
            owner = db.scalar(select(models.MigrationExecutionOwner).where(models.MigrationExecutionOwner.workspace_id == workspace_id, models.MigrationExecutionOwner.parent_id == migration.id))
            if owner is not None:
                db.delete(owner)
    elif action.action in {"resume_remaining", "retry_failed", "continue_incomplete"}:
        migration.state = "queued"
        migration.reason_code = None
        for item in db.scalars(select(models.StorageMigrationItem).where(models.StorageMigrationItem.migration_id == migration.id)):
            if action.action == "retry_failed" and item.outcome in {"failed", "source_changed"}:
                item.outcome = None
                item.generation += 1
            elif action.action in {"resume_remaining", "continue_incomplete"} and item.outcome == "canceled":
                item.outcome = None
                item.generation += 1
    elif action.action == "abandon_remaining":
        migration.state = "abandoned"
        migration.reason_code = "operator_canceled"
        for transfer in list(db.scalars(select(models.StorageTransfer).where(models.StorageTransfer.workspace_id == workspace_id, models.StorageTransfer.migration_id == migration.id))):
            try:
                StorageRepository(db, workspace_id).mark_transfer_reclaim_pending(transfer.id)
            except StorageRepositoryError:
                pass
        owner = db.scalar(select(models.MigrationExecutionOwner).where(models.MigrationExecutionOwner.workspace_id == workspace_id, models.MigrationExecutionOwner.parent_id == migration.id))
        if owner is not None:
            db.delete(owner)
        record_activity(db, workspace_id=workspace_id, action="abandoned", subject_type="storage_migration", subject_id=migration.id, profile_id=None, details={"message": "Remaining migration work abandoned."})
    migration.version += 1
    if action.action == "cancel":
        record_activity(db, workspace_id=workspace_id, action="cancel_requested", subject_type="storage_migration", subject_id=migration.id, profile_id=None, details={"message": "Migration cancellation requested."})
    elif action.action != "abandon_remaining":
        record_activity(db, workspace_id=workspace_id, action="continued", subject_type="storage_migration", subject_id=migration.id, profile_id=None, details={"message": "Migration action accepted."})
    db.commit()
    detail = _detail(db, migration)
    return {"kind": "detail", "detail": detail.model_dump(mode="json"), "announcement": _announcement("Migration action accepted.").model_dump(mode="json")}


@migration_router.post("/storage-migrations/preview")
def preview_route(request: StorageMigrationPreviewRequestV1, db: Session = Depends(get_db)):
    _require_gate(db)
    workspace = current_workspace(db)
    try:
        _begin_immediate(db)
        return create_preview(db, workspace.id, request).model_dump(mode="json")
    except (StorageNotFound, MigrationPreviewConflict, StorageRepositoryError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "storage_migration_preview_conflict", "message": str(exc)}) from exc


@migration_router.post("/storage-migrations/admit")
def admit_route(request: StorageMigrationAdmitRequestV1, db: Session = Depends(get_db)):
    _require_gate(db)
    workspace = current_workspace(db)
    try:
        _begin_immediate(db)
        result = admit_preview(db, workspace.id, request)
        return JSONResponse(status_code=409 if result.get("kind") == "conflict" else 202, content=result)
    except (StorageNotFound, MigrationPreviewConflict) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "storage_migration_preview_conflict", "message": str(exc)}) from exc


@migration_router.get("/storage-migrations/{migration_id}")
def detail_route(migration_id: UUID, db: Session = Depends(get_db)):
    _require_gate(db)
    workspace = current_workspace(db)
    migration = db.scalar(select(models.StorageMigration).where(models.StorageMigration.id == str(migration_id), models.StorageMigration.workspace_id == workspace.id))
    if migration is None:
        raise HTTPException(status_code=404, detail="storage migration not found")
    return _detail(db, migration).model_dump(mode="json")


@migration_router.post("/storage-migrations/{migration_id}/actions")
def action_route(migration_id: UUID, request: dict[str, Any], db: Session = Depends(get_db)):
    _require_gate(db)
    workspace = current_workspace(db)
    try:
        _begin_immediate(db)
        return perform_action(db, workspace.id, str(migration_id), request)
    except ValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except HTTPException:
        db.rollback()
        raise
    except (StorageNotFound, MigrationPreviewConflict, PolicyConflict, StorageRepositoryError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "storage_migration_conflict", "message": str(exc)}) from exc
