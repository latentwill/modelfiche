from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .contracts import StoragePolicyCandidateV1, StoragePolicyPatchV1, StoragePolicyV1
from .repository import PolicyConflict, StorageNotFound, StorageRepository, StorageRepositoryError


_WRITE_CAPABILITIES = {"managed_write", "write", "storage_write"}
_READ_CAPABILITIES = {"basic_read", "conditional_read", "versioned_read", "read"}


def default_policy() -> StoragePolicyV1:
    """Return the safe projection before an operator selects a writable source."""
    return StoragePolicyV1(
        version=1,
        desired_provider="local",
        resolved_provider="local",
        selection_origin="automatic",
        write_source_id=None,
        fal_local_fallback=True,
        blocked_reason=None,
        candidates=[],
    )


def list_policy_candidates(db: Session, workspace_id: str) -> list[StoragePolicyCandidateV1]:
    """Build a deterministic projection of Local roots and named S3 sources.

    Candidate state is derived only from persisted source/root identity and the
    latest capability evidence.  The source name is display-only and never acts
    as an authority for selection.
    """
    now = datetime.now(timezone.utc)
    candidates: list[tuple[tuple[str, str], StoragePolicyCandidateV1]] = []

    roots = db.scalars(
        select(models.LocalRoot)
        .where(models.LocalRoot.workspace_id == workspace_id)
        .order_by(models.LocalRoot.created_at, models.LocalRoot.id)
    )
    for root in roots:
        if root.availability == "available" and root.retired_at is None:
            state = "write_ready"
            blocked = None
        elif root.availability in {"pending", "checking"}:
            state = "pending"
            blocked = "local_root_check_pending"
        else:
            state = "failed"
            blocked = "local_root_unavailable"
        candidates.append(
            (
                (str(root.created_at), root.id),
                StoragePolicyCandidateV1(
                    source_id=UUID(root.id),
                    label=f"Local: {root.canonical_path}",
                    state=state,
                    capabilities=["local_read", "managed_write"] if state == "write_ready" else [],
                    blocked_reason=blocked,
                ),
            )
        )

    sources = db.scalars(
        select(models.ImportSource)
        .where(models.ImportSource.workspace_id == workspace_id)
        .order_by(models.ImportSource.created_at, models.ImportSource.id)
    )
    for source in sources:
        capability_rows = list(
            db.scalars(
                select(models.SourceCapability)
                .where(models.SourceCapability.source_id == source.id)
                .order_by(models.SourceCapability.expires_at.desc(), models.SourceCapability.created_at.desc(), models.SourceCapability.id)
            )
        )
        latest: dict[str, models.SourceCapability] = {}
        for row in capability_rows:
            if row.capability not in latest:
                latest[row.capability] = row
        available: set[str] = set()
        failed: set[str] = set()
        for capability, row in latest.items():
            expiry = row.expires_at
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if row.state in {"ready", "available", "supported", "ok"} and (expiry is None or expiry > now):
                available.add(capability)
            else:
                failed.add(capability)
        capabilities = sorted(available)
        if not source.is_active:
            state = "failed"
            blocked = "source_inactive"
        elif available & _WRITE_CAPABILITIES:
            state = "write_ready"
            blocked = None
        elif available & _READ_CAPABILITIES:
            state = "read_only"
            blocked = "managed_write_unavailable"
        elif failed:
            state = "failed"
            blocked = "capability_probe_failed"
        else:
            state = "pending"
            blocked = "capability_check_pending"
        candidates.append(
            (
                (str(source.created_at), source.id),
                StoragePolicyCandidateV1(
                    source_id=UUID(source.id),
                    label=source.name,
                    state=state,
                    capabilities=capabilities,
                    blocked_reason=blocked,
                ),
            )
        )

    candidates.sort(key=lambda item: item[0])
    return [candidate for _, candidate in candidates]


def project_policy(db: Session, workspace_id: str) -> StoragePolicyV1:
    candidates = list_policy_candidates(db, workspace_id)
    row = db.scalar(select(models.StoragePolicy).where(models.StoragePolicy.workspace_id == workspace_id))
    if row is None:
        return default_policy().model_copy(update={"candidates": candidates})

    selected_id = row.write_source_id
    if row.desired_provider == "s3" and row.selection_origin == "automatic" and selected_id is None:
        ready_sources = [candidate for candidate in candidates if candidate.state == "write_ready" and not candidate.label.startswith("Local:")]
        if len(ready_sources) == 1:
            selected_id = str(ready_sources[0].source_id)
    selected = next((candidate for candidate in candidates if str(candidate.source_id) == selected_id), None)
    selected_ready = selected is not None and selected.state == "write_ready"
    if row.desired_provider == "s3" and row.selection_origin == "automatic" and row.write_source_id is None:
        ready_sources = [candidate for candidate in candidates if candidate.state == "write_ready" and not candidate.label.startswith("Local:")]
        if len(ready_sources) == 1:
            resolved: Literal["local", "s3"] = "s3"
            blocked = row.blocked_reason
        else:
            resolved = "local" if row.fal_local_fallback else "s3"
            blocked = row.blocked_reason or ("multiple_write_sources" if len(ready_sources) > 1 else "write_source_unavailable")
    elif row.desired_provider == "s3" and selected_ready:
        resolved = "s3"
        blocked = row.blocked_reason
    elif row.desired_provider == "s3":
        resolved = "local" if row.fal_local_fallback else "s3"
        blocked = row.blocked_reason or "write_source_unavailable"
    else:
        resolved = "local"
        blocked = row.blocked_reason
    return StoragePolicyV1(
        version=max(1, row.version),
        desired_provider=row.desired_provider,
        resolved_provider=resolved,
        selection_origin=row.selection_origin,
        write_source_id=UUID(selected_id) if selected_id else None,
        fal_local_fallback=row.fal_local_fallback,
        blocked_reason=blocked,
        candidates=candidates,
    )


def patch_policy(db: Session, workspace_id: str, patch: StoragePolicyPatchV1) -> StoragePolicyV1:
    """Apply a policy patch with a single expected-version compare-and-swap."""
    current = project_policy(db, workspace_id)
    if patch.expected_version != current.version:
        raise PolicyConflict("storage policy version is stale")
    if patch.desired_provider == "local" and patch.write_source_id is not None:
        raise StorageRepositoryError("Local policy cannot select an S3 write source")
    if patch.desired_provider == "s3":
        if patch.write_source_id is None:
            raise StorageRepositoryError("S3 policy requires a named write source")
        candidate = next(
            (item for item in current.candidates if item.source_id == patch.write_source_id),
            None,
        )
        if candidate is None:
            raise StorageNotFound("storage policy source")
        if candidate.state != "write_ready":
            raise PolicyConflict("storage policy source is not write-ready")

    row = db.scalar(select(models.StoragePolicy).where(models.StoragePolicy.workspace_id == workspace_id))
    if row is None:
        row = models.StoragePolicy(
            workspace_id=workspace_id,
            version=1,
            desired_provider=patch.desired_provider,
            resolved_provider=patch.desired_provider,
            write_source_id=str(patch.write_source_id) if patch.write_source_id else None,
            selection_origin="operator",
            fal_local_fallback=patch.fal_local_fallback,
            blocked_reason=None,
        )
        db.add(row)
    else:
        if row.version != patch.expected_version:
            raise PolicyConflict("storage policy version is stale")
        row.version += 1
        row.desired_provider = patch.desired_provider
        row.resolved_provider = patch.desired_provider
        row.write_source_id = str(patch.write_source_id) if patch.write_source_id else None
        row.selection_origin = "operator"
        row.fal_local_fallback = patch.fal_local_fallback
        row.blocked_reason = None
    db.flush()
    return project_policy(db, workspace_id)


def policy_snapshot(db: Session, workspace_id: str, *, operation_id: str, actor_profile_id: str | None = None) -> models.StoragePolicySnapshot:
    """Persist the immutable policy projection used by an admitted operation."""
    projection = project_policy(db, workspace_id)
    source = db.get(models.ImportSource, str(projection.write_source_id)) if projection.write_source_id else None
    local_root_id = next(
        (str(candidate.source_id) for candidate in projection.candidates if candidate.label.startswith("Local:") and candidate.state == "write_ready"),
        None,
    )
    snapshot = models.StoragePolicySnapshot(
        workspace_id=workspace_id,
        contract_version=1,
        desired_provider=projection.desired_provider,
        resolved_provider=projection.resolved_provider,
        origin_source_id=None,
        origin_source_fingerprint=None,
        write_source_id=source.id if source is not None else None,
        write_source_fingerprint=source.identity_fingerprint if source is not None else None,
        write_managed_prefix=source.managed_prefix if source is not None else None,
        local_root_id=local_root_id,
        fal_local_fallback=projection.fal_local_fallback,
        operation_id=operation_id,
        actor_profile_id=actor_profile_id,
    )
    db.add(snapshot)
    db.flush()
    return snapshot
