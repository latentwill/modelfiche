from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from secrets import token_hex
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import models


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RELATIVE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.?/)[^\\\x00]+$")
_MAX_FAL_SUBJECTS = 100
_MAX_FAL_ARTIFACTS = 100
_FAL_HANDOFF_ASSURANCES = {"locally_verified", "provider_verified"}
_FAL_INTENT_STATES = {"claimed", "submitted", "accepted", "completed", "failed", "ambiguous", "canceled"}
_FAL_ARTIFACT_STATES = {"planned", "observed", "committed", "failed", "quarantined"}


class StorageRepositoryError(RuntimeError):
    """Base storage-repository exception."""


class StorageNotFound(StorageRepositoryError):
    """Raised when a workspace-scoped storage row does not exist."""


class PublicationCommitError(StorageRepositoryError):
    """Raised when a physical publication cannot become an AssetLocation."""


class PolicyConflict(StorageRepositoryError):
    """Raised when a policy mutation loses its expected-version compare-and-swap."""


class MigrationPreviewConflict(StorageRepositoryError):
    """Raised when a migration preview has expired or lost its version CAS."""


@dataclass(frozen=True, slots=True)
class PublicationReceiptInput:
    attempt_id: str
    provider_locator: str
    sha256: str
    size: int
    commit_fence: str
    version_id: str | None = None
    etag: str | None = None


class StorageRepository:
    """Workspace-scoped persistence boundary for all durable storage effects."""

    def __init__(self, session: Session, workspace_id: str):
        self.session = session
        self.workspace_id = workspace_id

    def create_local_root(
        self,
        *,
        kind: str,
        canonical_path: str,
        fingerprint: str,
        owner_uid: int,
        mode: int,
    ) -> models.LocalRoot:
        root = models.LocalRoot(
            workspace_id=self.workspace_id,
            kind=kind,
            canonical_path=canonical_path,
            fingerprint=fingerprint,
            owner_uid=owner_uid,
            mode=mode,
        )
        self.session.add(root)
        self.session.flush()
        return root

    def get_or_create_content_blob(self, *, sha256: str, size: int, mime_type: str) -> models.ContentBlob:
        digest = self._digest(sha256)
        if size < 0:
            raise StorageRepositoryError("content size must be non-negative")
        blob = self.session.scalar(select(models.ContentBlob).where(models.ContentBlob.sha256 == digest))
        if blob is not None:
            if blob.size != size or blob.mime_type != mime_type:
                raise PublicationCommitError("content blob digest conflicts with verified metadata")
            return blob
        blob = models.ContentBlob(sha256=digest, size=size, mime_type=mime_type)
        self.session.add(blob)
        self.session.flush()
        return blob
    def attach_verified_remote_location(
        self,
        *,
        asset_id: str,
        source_id: str,
        object_key: str,
        etag: str | None,
        size: int,
        sha256: str,
        mime_type: str,
        modified_at: datetime | None = None,
    ) -> models.AssetLocation:
        asset = self.get_asset_revision(asset_id)
        source = self._source(source_id)
        blob = self.get_or_create_content_blob(sha256=sha256, size=size, mime_type=mime_type)
        location = self.session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.asset_id == asset.id,
                models.AssetLocation.source_id == source.id,
                models.AssetLocation.object_key == object_key,
            )
        )
        if location is None:
            location = models.AssetLocation(asset_id=asset.id, provider="s3")
            self.session.add(location)
        location.workspace_id = self.workspace_id
        location.provider = "s3"
        location.uri = f"s3://{source.id}/{object_key}"
        location.bucket = source.bucket
        location.object_key = object_key
        location.etag = etag
        location.source_id = source.id
        location.source_revision_fingerprint = source.identity_fingerprint
        location.size = size
        location.verified_size = size
        location.verified_sha256 = blob.sha256
        location.verification_state = "available"
        location.modified_at = modified_at
        location.last_verified_at = datetime.now(timezone.utc)
        location.hydration_state = "remote"
        self.session.flush()
        asset.sha256 = blob.sha256
        asset.content_blob_id = blob.id
        if asset.preferred_location_id is None:
            asset.preferred_location_id = location.id
        if asset.origin_location_id is None:
            asset.origin_location_id = location.id
        self.session.flush()
        return location
    def attach_verified_local_location(
        self,
        *,
        asset_id: str,
        uri: str,
        size: int,
        sha256: str,
        mime_type: str,
    ) -> models.AssetLocation:
        asset = self.get_asset_revision(asset_id)
        blob = self.get_or_create_content_blob(sha256=sha256, size=size, mime_type=mime_type)
        location = self.session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.asset_id == asset.id,
                models.AssetLocation.provider == "local",
                models.AssetLocation.uri == uri,
            )
        )
        if location is None:
            location = models.AssetLocation(asset_id=asset.id, provider="local", uri=uri)
            self.session.add(location)
        location.workspace_id = self.workspace_id
        location.size = size
        location.verified_size = size
        location.verified_sha256 = blob.sha256
        location.verification_state = "available"
        location.last_verified_at = datetime.now(timezone.utc)
        location.hydration_state = "hydrated"
        self.session.flush()
        asset.sha256 = blob.sha256
        asset.content_blob_id = blob.id
        if asset.preferred_location_id is None:
            asset.preferred_location_id = location.id
        if asset.origin_location_id is None:
            asset.origin_location_id = location.id
        self.session.flush()
        return location



    def reconcile_policy(
        self,
        *,
        expected_version: int,
        desired_provider: Literal["local", "s3"],
        resolved_provider: Literal["local", "s3"],
        selection_origin: Literal["automatic", "operator"],
        write_source_id: str | None,
        fal_local_fallback: bool,
        blocked_reason: str | None = None,
    ) -> models.StoragePolicy:
        if expected_version < 0:
            raise PolicyConflict("policy version must be non-negative")
        if desired_provider not in {"local", "s3"} or resolved_provider not in {"local", "s3"}:
            raise StorageRepositoryError("storage policy provider must be local or s3")
        if selection_origin not in {"automatic", "operator"}:
            raise StorageRepositoryError("storage policy selection origin is invalid")
        if write_source_id is not None:
            self._source(write_source_id)
        if desired_provider == "s3" and write_source_id is None:
            raise StorageRepositoryError("S3 policy requires a write source")
        policy = self.session.scalar(
            select(models.StoragePolicy).where(models.StoragePolicy.workspace_id == self.workspace_id)
        )
        if policy is None:
            if expected_version != 0:
                raise PolicyConflict("storage policy does not exist at requested version")
            policy = models.StoragePolicy(
                workspace_id=self.workspace_id,
                version=1,
                desired_provider=desired_provider,
                resolved_provider=resolved_provider,
                write_source_id=write_source_id,
                selection_origin=selection_origin,
                fal_local_fallback=fal_local_fallback,
                blocked_reason=blocked_reason,
            )
            self.session.add(policy)
        else:
            if policy.version != expected_version:
                raise PolicyConflict("storage policy version is stale")
            policy.version += 1
            policy.desired_provider = desired_provider
            policy.resolved_provider = resolved_provider
            policy.write_source_id = write_source_id
            policy.selection_origin = selection_origin
            policy.fal_local_fallback = fal_local_fallback
            policy.blocked_reason = blocked_reason
        self.session.flush()
        return policy

    def reserve_capacity(
        self,
        *,
        transfer_id: str,
        root_id: str | None,
        kind: Literal["permanent", "temporary", "spool", "cache"],
        reserved_bytes: int,
    ) -> models.StorageReservation:
        transfer = self._transfer(transfer_id)
        if reserved_bytes <= 0:
            raise StorageRepositoryError("reservation bytes must be positive")
        if root_id is not None:
            self._root(root_id)
        reservation = models.StorageReservation(
            workspace_id=self.workspace_id,
            transfer_id=transfer.id,
            root_id=root_id,
            kind=kind,
            reserved_bytes=reserved_bytes,
            state="active",
            generation=transfer.generation,
            lease_owner=transfer.lease_owner,
            lease_expires_at=transfer.lease_expires_at,
        )
        self.session.add(reservation)
        self.session.flush()
        return reservation

    def mark_transfer_reclaim_pending(self, transfer_id: str) -> int:
        transfer = self._transfer(transfer_id)
        if transfer.state == "committed":
            raise StorageRepositoryError("committed transfers cannot be reclaimed")
        transfer.state = "reclaim_pending"
        reservations = list(
            self.session.scalars(
                select(models.StorageReservation).where(
                    models.StorageReservation.workspace_id == self.workspace_id,
                    models.StorageReservation.transfer_id == transfer.id,
                    models.StorageReservation.state == "active",
                )
            )
        )
        for reservation in reservations:
            reservation.state = "reclaim_pending"
            reservation.generation += 1
        self.session.flush()
        return len(reservations)

    def create_migration_preview(
        self,
        *,
        scope_kind: Literal["all_images", "project", "unassigned"],
        project_id: str | None,
        destination_provider: Literal["local", "s3"],
        destination_root_id: str | None = None,
        destination_source_id: str | None = None,
        expires_in: timedelta,
        items: list[dict[str, object]],
        predecessor_id: str | None = None,
        predecessor_version: int | None = None,
        expected_owner_epoch: int | None = None,
        frozen_fields: dict[str, object] | None = None,
        readiness_state: str = "ready",
        disabled_reason: str | None = None,
    ) -> models.StorageMigrationPreview:
        if expires_in.total_seconds() <= 0:
            raise MigrationPreviewConflict("preview expiry must be in the future")
        if scope_kind not in {"all_images", "project", "unassigned"}:
            raise StorageRepositoryError("migration scope is invalid")
        if scope_kind == "project":
            if project_id is None:
                raise StorageRepositoryError("project scope requires a project")
            project = self.session.scalar(
                select(models.Project).where(
                    models.Project.id == project_id,
                    models.Project.workspace_id == self.workspace_id,
                )
            )
            if project is None:
                raise StorageNotFound("project")
        elif project_id is not None:
            raise StorageRepositoryError("only project scope may contain a project ID")
        self._validate_destination(destination_provider, destination_root_id, destination_source_id)
        if predecessor_id is not None:
            predecessor = self._migration(predecessor_id)
            if predecessor_version is None or predecessor.version != predecessor_version:
                raise MigrationPreviewConflict("predecessor migration version is stale")
            owner = self.session.scalar(
                select(models.MigrationExecutionOwner).where(
                    models.MigrationExecutionOwner.workspace_id == self.workspace_id,
                    models.MigrationExecutionOwner.parent_id == predecessor.id,
                )
            )
            if owner is None or expected_owner_epoch is None or owner.owner_epoch != expected_owner_epoch:
                raise MigrationPreviewConflict("predecessor migration owner is stale")
        eligible_count = sum(bool(item.get("eligible")) for item in items)
        known_sizes = [int(item["known_size"]) for item in items if item.get("known_size") is not None]
        if any(size < 0 for size in known_sizes):
            raise StorageRepositoryError("preview item size must be non-negative")
        preview = models.StorageMigrationPreview(
            workspace_id=self.workspace_id,
            expires_at=models.utcnow() + expires_in,
            scope_kind=scope_kind,
            project_id=project_id,
            destination_provider=destination_provider,
            destination_source_id=destination_source_id,
            destination_root_id=destination_root_id,
            predecessor_id=predecessor_id,
            predecessor_version=predecessor_version,
            expected_owner_epoch=expected_owner_epoch,
            frozen_fields=dict(frozen_fields or {}),
            eligible_count=eligible_count,
            excluded_count=len(items) - eligible_count,
            known_bytes=sum(known_sizes),
            unknown_bytes=sum(1 for item in items if item.get("known_size") is None),
            worst_case_bytes=sum(known_sizes),
            readiness_state=readiness_state,
            disabled_reason=disabled_reason,
        )
        self.session.add(preview)
        self.session.flush()
        for ordinal, item in enumerate(items):
            asset = self.get_asset_revision(str(item["asset_id"]))
            source_location_id = item.get("source_location_id")
            if source_location_id is not None:
                self._location(str(source_location_id))
            content_blob_id = item.get("content_blob_id") or asset.content_blob_id
            preview_item = models.StorageMigrationPreviewItem(
                preview_id=preview.id,
                ordinal=ordinal,
                asset_id=asset.id,
                content_blob_id=str(content_blob_id) if content_blob_id is not None else None,
                source_location_id=str(source_location_id) if source_location_id is not None else None,
                selected_revision_fingerprint=(
                    str(item["selected_revision_fingerprint"])
                    if item.get("selected_revision_fingerprint") is not None
                    else None
                ),
                destination_provider=destination_provider,
                destination_source_id=destination_source_id,
                destination_root_id=destination_root_id,
                eligible=bool(item.get("eligible")),
                reason_code=str(item["reason_code"]) if item.get("reason_code") is not None else None,
                known_size=int(item["known_size"]) if item.get("known_size") is not None else None,
            )
            self.session.add(preview_item)
        self.session.flush()
        return preview

    def consume_migration_preview(
        self,
        preview_id: str,
        *,
        expected_version: int,
    ) -> models.StorageMigrationPreview:
        preview = self._migration_preview(preview_id)
        if preview.state != "open":
            raise MigrationPreviewConflict("migration preview is no longer open")
        if preview.expires_at <= models.utcnow():
            preview.state = "expired"
            self.session.flush()
            raise MigrationPreviewConflict("migration preview has expired")
        if preview.version != expected_version:
            raise MigrationPreviewConflict("migration preview version is stale")
        preview.state = "consumed"
        preview.version += 1
        self.session.flush()
        return preview

    def begin_transfer(
        self,
        *,
        asset_id: str,
        destination_provider: Literal["local", "s3"],
        destination_root_id: str | None = None,
        destination_source_id: str | None = None,
        source_location_id: str | None = None,
        snapshot_id: str | None = None,
        expected_sha256: str,
        expected_size: int,
        logical_operation_id: str | None = None,
        migration_id: str | None = None,
        migration_owner_epoch: int | None = None,
    ) -> models.StorageTransfer:
        asset = self.get_asset_revision(asset_id)
        digest = self._digest(expected_sha256)
        if expected_size < 0:
            raise StorageRepositoryError("expected size must be non-negative")
        if destination_provider == "local":
            if destination_root_id is None or destination_source_id is not None:
                raise StorageRepositoryError("local transfers require one local root and no S3 source")
            self._root(destination_root_id)
        elif destination_provider == "s3":
            if destination_source_id is None or destination_root_id is not None:
                raise StorageRepositoryError("S3 transfers require one S3 source and no local root")
            self._source(destination_source_id)
        else:
            raise StorageRepositoryError("destination provider must be local or s3")
        if source_location_id is not None:
            self._location(source_location_id)
        if snapshot_id is not None:
            self._snapshot(snapshot_id)
        if migration_id is not None:
            migration = self._migration(migration_id)
            if migration.workspace_id != self.workspace_id:
                raise StorageNotFound("migration")
        transfer = models.StorageTransfer(
            workspace_id=self.workspace_id,
            snapshot_id=snapshot_id,
            asset_id=asset.id,
            source_location_id=source_location_id,
            destination_provider=destination_provider,
            destination_source_id=destination_source_id,
            destination_root_id=destination_root_id,
            logical_operation_id=logical_operation_id or f"transfer:{asset.id}:{uuid4()}",
            state="pending",
            generation=0,
            commit_fence=token_hex(24),
            expected_sha256=digest,
            expected_size=expected_size,
            migration_id=migration_id,
            migration_owner_epoch=migration_owner_epoch,
        )
        self.session.add(transfer)
        self.session.flush()
        return transfer

    def begin_publication_attempt(self, *, transfer_id: str, final_locator: str) -> models.PublicationAttempt:
        transfer = self._transfer(transfer_id)
        if transfer.state not in {"pending", "streaming", "uploaded_unverified", "verified"}:
            raise PublicationCommitError("transfer is not publishable")
        attempt = models.PublicationAttempt(
            transfer_id=transfer.id,
            generation=transfer.generation,
            nonce=token_hex(24),
            publication_fence=transfer.commit_fence,
            final_locator=final_locator,
            expected_sha256=transfer.expected_sha256,
            expected_size=transfer.expected_size,
            state="pending",
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    def record_publication_receipt(
        self,
        *,
        attempt_id: str,
        provider_locator: str,
        sha256: str,
        size: int,
        commit_fence: str,
        version_id: str | None = None,
        etag: str | None = None,
    ) -> models.PublicationReceipt:
        attempt = self._attempt(attempt_id)
        transfer = self._transfer(attempt.transfer_id)
        digest = self._digest(sha256)
        if digest != transfer.expected_sha256 or size != transfer.expected_size:
            raise PublicationCommitError("receipt bytes do not match transfer expectation")
        if commit_fence != transfer.commit_fence:
            raise PublicationCommitError("receipt commit fence is stale")
        existing = self.session.scalar(select(models.PublicationReceipt).where(models.PublicationReceipt.attempt_id == attempt.id))
        if existing is not None:
            if existing.sha256 != digest or existing.size != size or existing.commit_fence != commit_fence:
                raise PublicationCommitError("attempt already has a different receipt")
            return existing
        receipt = models.PublicationReceipt(
            attempt_id=attempt.id,
            provider_locator=provider_locator,
            version_id=version_id,
            etag=etag,
            size=size,
            sha256=digest,
            publication_fence=attempt.publication_fence,
            commit_fence=commit_fence,
        )
        attempt.state = "verified"
        transfer.state = "verified"
        self.session.add(receipt)
        self.session.flush()
        return receipt

    def commit_publication(
        self,
        *,
        transfer_id: str,
        attempt_id: str,
        asset_id: str,
        local_root_id: str | None = None,
        relative_path: str | None = None,
        source_id: str | None = None,
        object_key: str | None = None,
    ) -> models.AssetLocation:
        transfer = self._transfer(transfer_id)
        attempt = self._attempt(attempt_id)
        asset = self.get_asset_revision(asset_id)
        if attempt.transfer_id != transfer.id or transfer.asset_id != asset.id:
            raise PublicationCommitError("publication does not belong to the requested asset transfer")
        receipt = self.session.scalar(select(models.PublicationReceipt).where(models.PublicationReceipt.attempt_id == attempt.id))
        if receipt is None:
            raise PublicationCommitError("publication receipt is required")
        self._validate_current_receipt(transfer, attempt, receipt)
        self._validate_migration_owner(transfer)
        blob = self.get_or_create_content_blob(
            sha256=receipt.sha256,
            size=receipt.size,
            mime_type=asset.mime_type or "application/octet-stream",
        )
        if asset.content_blob_id is not None and asset.content_blob_id != blob.id:
            raise PublicationCommitError("immutable asset cannot be repointed to different content")
        location = self._new_verified_location(
            transfer=transfer,
            asset=asset,
            receipt=receipt,
            local_root_id=local_root_id,
            relative_path=relative_path,
            source_id=source_id,
            object_key=object_key,
        )
        asset.content_blob_id = blob.id
        asset.sha256 = blob.sha256
        asset.preferred_location_id = location.id
        transfer.state = "committed"
        attempt.state = "committed"
        self.session.flush()
        return location

    def commit_receipt(
        self,
        receipt: models.PublicationReceipt,
        set_effect_epoch: Callable[[], None],
        *,
        asset_id: str | None = None,
        local_root_id: str | None = None,
        relative_path: str | None = None,
        source_id: str | None = None,
        object_key: str | None = None,
    ) -> models.AssetLocation:
        """Commit an already-verified receipt and set the caller's effect epoch atomically."""
        attempt = self._attempt(receipt.attempt_id)
        transfer = self._transfer(attempt.transfer_id)
        location = self.commit_publication(
            transfer_id=transfer.id,
            attempt_id=attempt.id,
            asset_id=asset_id or self._required(transfer.asset_id, "transfer asset"),
            local_root_id=local_root_id or transfer.destination_root_id,
            relative_path=relative_path or attempt.final_locator,
            source_id=source_id or transfer.destination_source_id,
            object_key=object_key or attempt.final_locator,
        )
        set_effect_epoch()
        self.session.flush()
        return location

    def compatible_locations(self, asset_id: str, *, preferred_first: bool = True) -> list[models.AssetLocation]:
        asset = self.get_asset_revision(asset_id)
        if asset.content_blob_id is None:
            return []
        blob = self.session.get(models.ContentBlob, asset.content_blob_id)
        if blob is None:
            raise PublicationCommitError("asset references missing content blob")
        locations = list(
            self.session.scalars(
                select(models.AssetLocation).where(
                    models.AssetLocation.asset_id == asset.id,
                    or_(
                        models.AssetLocation.workspace_id == self.workspace_id,
                        (models.AssetLocation.provider == "local") & models.AssetLocation.workspace_id.is_(None),
                    ),
                    models.AssetLocation.verification_state == "available",
                    models.AssetLocation.verified_sha256 == blob.sha256,
                )
            )
        )
        preferred_provider = self._preferred_provider(asset, locations) if locations else None
        locations.sort(
            key=lambda row: (
                0 if preferred_first and row.id == asset.preferred_location_id else 1,
                0 if row.provider == preferred_provider else 1,
                -(row.last_verified_at.timestamp() if row.last_verified_at else 0),
                row.id,
            )
        )
        return locations

    def select_delivery_locations(self, asset_id: str) -> tuple[models.AssetLocation | None, models.AssetLocation | None]:
        asset = self.get_asset_revision(asset_id)
        if asset.content_blob_id is None:
            # Browser-folder imports predate content-blob publication. Keep
            # verified local copies deliverable while remote W&B locations
            # remain addressable through their source-backed object keys.
            locations = list(
                self.session.scalars(
                    select(models.AssetLocation).where(
                        models.AssetLocation.asset_id == asset.id,
                        or_(
                            models.AssetLocation.workspace_id == self.workspace_id,
                            (models.AssetLocation.provider == "local") & models.AssetLocation.workspace_id.is_(None),
                        ),
                        models.AssetLocation.verification_state.in_(("verified", "available")),
                        or_(
                            models.AssetLocation.provider == "s3",
                            (models.AssetLocation.provider == "local") & (models.AssetLocation.hydration_state == "hydrated"),
                        ),
                    )
                )
            )
            locations.sort(key=lambda row: (-(row.last_verified_at.timestamp() if row.last_verified_at else 0), row.id))
            return (locations[0], locations[1] if len(locations) > 1 else None) if locations else (None, None)
        locations = self.compatible_locations(asset.id)
        return (locations[0], locations[1] if len(locations) > 1 else None) if locations else (None, None)

    def get_asset_revision(self, asset_id: str) -> models.Asset:
        asset = self.session.scalar(select(models.Asset).where(models.Asset.id == asset_id, models.Asset.workspace_id == self.workspace_id))
        if asset is None:
            raise StorageNotFound("asset")
        return asset
    def create_fal_admission(
        self,
        *,
        snapshot_id: str,
        checkpoint_revision_id: str,
        endpoint_id: str,
        spool_root_id: str,
        expected_artifact_count: int,
        spool_grant_bytes: int,
        permanent_reservation_bytes: int,
        handoff_assurance: Literal["locally_verified", "provider_verified"],
        billing_acknowledged: bool = False,
        eval_run_id: str | None = None,
        staging_source_id: str | None = None,
    ) -> models.FalAdmission:
        """Persist the immutable, pre-provider FAL admission snapshot.

        This method intentionally performs no provider I/O.  Every foreign key and
        workspace boundary is checked before the row is flushed, leaving callers
        with a short transaction that can be committed before a billable POST.
        """
        snapshot = self._snapshot(snapshot_id)
        if not isinstance(endpoint_id, str) or not endpoint_id or len(endpoint_id) > 240:
            raise StorageRepositoryError("FAL endpoint is required")
        if handoff_assurance not in _FAL_HANDOFF_ASSURANCES:
            raise StorageRepositoryError("FAL handoff assurance is invalid")
        if not isinstance(billing_acknowledged, bool):
            raise StorageRepositoryError("FAL billing acknowledgement must be boolean")
        if handoff_assurance == "locally_verified" and not billing_acknowledged:
            raise StorageRepositoryError("locally verified FAL handoff requires billing acknowledgement")
        if handoff_assurance == "provider_verified" and billing_acknowledged:
            raise StorageRepositoryError("provider verified FAL handoff cannot include billing acknowledgement")
        if (
            not isinstance(expected_artifact_count, int)
            or isinstance(expected_artifact_count, bool)
            or not 1 <= expected_artifact_count <= _MAX_FAL_ARTIFACTS
        ):
            raise StorageRepositoryError("FAL expected artifact count is out of range")
        for value, name in (
            (spool_grant_bytes, "spool grant"),
            (permanent_reservation_bytes, "permanent reservation"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise StorageRepositoryError(f"FAL {name} bytes must be positive")
        root = self._root(spool_root_id)
        if root.kind != "spool":
            raise StorageRepositoryError("FAL spool root must be a spool root")
        if snapshot.local_root_id is not None and snapshot.resolved_provider == "local":
            if snapshot.local_root_id != root.id:
                raise StorageRepositoryError("FAL spool root differs from policy snapshot")
        self._checkpoint_revision(checkpoint_revision_id)
        if eval_run_id is not None:
            self._eval_run(eval_run_id)
        if staging_source_id is not None:
            self._source(staging_source_id)
        admission = models.FalAdmission(
            workspace_id=self.workspace_id,
            eval_run_id=eval_run_id,
            snapshot_id=snapshot.id,
            checkpoint_revision_id=checkpoint_revision_id,
            staging_source_id=staging_source_id,
            endpoint_id=endpoint_id,
            version=0,
            state="unsubmitted",
            handoff_assurance=handoff_assurance,
            billing_acknowledged_at=models.utcnow() if billing_acknowledged else None,
            expected_artifact_count=expected_artifact_count,
            spool_root_id=root.id,
            spool_grant_bytes=spool_grant_bytes,
            permanent_reservation_bytes=permanent_reservation_bytes,
            dispatch_fence=token_hex(24),
        )
        self.session.add(admission)
        self.session.flush()
        return admission

    def create_fal_subjects(
        self,
        *,
        admission_id: str,
        subjects: Sequence[Mapping[str, Any]],
    ) -> list[models.FalSubject]:
        """Materialize all immutable FAL subjects and their expected ordinals."""
        admission = self._fal_admission(admission_id)
        if admission.state != "unsubmitted":
            raise StorageRepositoryError("FAL subjects can only be materialized before submission")
        rows = list(subjects)
        if not rows:
            raise StorageRepositoryError("FAL admission requires at least one subject")
        if len(rows) > _MAX_FAL_SUBJECTS:
            raise StorageRepositoryError("FAL subject count exceeds the limit")
        existing_count = self.session.scalar(
            select(models.FalSubject.id).where(models.FalSubject.admission_id == admission.id).limit(1)
        )
        if existing_count is not None:
            raise StorageRepositoryError("FAL subjects are immutable once materialized")
        total_expected = 0
        keys: set[str] = set()
        materialized: list[models.FalSubject] = []
        for ordinal, value in enumerate(rows):
            if not isinstance(value, Mapping):
                raise StorageRepositoryError("FAL subject must be an object")
            subject_key = value.get("subject_key")
            if not isinstance(subject_key, str) or not subject_key or len(subject_key) > 256:
                raise StorageRepositoryError("FAL subject key is invalid")
            if subject_key in keys:
                raise StorageRepositoryError("FAL subject keys must be unique")
            keys.add(subject_key)
            definition = value.get("definition")
            if not isinstance(definition, Mapping):
                raise StorageRepositoryError("FAL subject definition must be an object")
            expected = value.get("expected_output_count")
            if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
                raise StorageRepositoryError("FAL subject expected output count must be positive")
            total_expected += expected
            if total_expected > admission.expected_artifact_count:
                raise StorageRepositoryError("FAL subject counts exceed admission expectation")
            materialized.append(
                models.FalSubject(
                    admission_id=admission.id,
                    ordinal=ordinal,
                    subject_key=subject_key,
                    definition=dict(definition),
                    expected_output_count=expected,
                    generation=0,
                    state="pending",
                )
            )
        if total_expected != admission.expected_artifact_count:
            raise StorageRepositoryError("FAL subject counts must equal admission expectation")
        self.session.add_all(materialized)
        self.session.flush()
        return materialized

    def claim_fal_intent(
        self,
        subject_id: str,
        *,
        generation: int,
        request_digest: str,
        expected_dispatch_fence: str | None = None,
    ) -> models.FalSubmissionIntent:
        """Claim one immutable subject generation for at-most-once dispatch."""
        subject = self._fal_subject(subject_id)
        admission = self._fal_admission(subject.admission_id)
        if admission.state not in {"unsubmitted", "ready", "admitted"}:
            raise StorageRepositoryError("FAL admission is not claimable")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise StorageRepositoryError("FAL intent generation is invalid")
        digest = self._digest(request_digest)
        if expected_dispatch_fence is not None and expected_dispatch_fence != admission.dispatch_fence:
            raise StorageRepositoryError("FAL dispatch fence is stale")
        existing = self.session.scalar(
            select(models.FalSubmissionIntent).where(
                models.FalSubmissionIntent.subject_id == subject.id,
                models.FalSubmissionIntent.generation == generation,
            )
        )
        if existing is not None:
            if existing.request_digest != digest:
                raise StorageRepositoryError("FAL intent generation already has a different request")
            if subject.generation != generation:
                raise StorageRepositoryError("FAL subject generation is stale")
            return existing
        if generation != subject.generation:
            if generation == subject.generation + 1 and subject.state in {"failed", "retryable"}:
                subject.generation = generation
                subject.state = "pending"
            else:
                raise StorageRepositoryError("FAL subject generation is stale")
        intent = models.FalSubmissionIntent(
            admission_id=admission.id,
            subject_id=subject.id,
            generation=generation,
            request_digest=digest,
            state="claimed",
            submission_fence=token_hex(24),
        )
        subject.state = "claimed"
        self.session.add(intent)
        self.session.flush()
        return intent

    def transition_fal_intent(
        self,
        intent_id: str,
        *,
        generation: int,
        submission_fence: str,
        state: Literal["submitted", "accepted", "completed", "failed", "ambiguous", "canceled"],
        provider_request_id: str | None = None,
    ) -> models.FalSubmissionIntent:
        """Advance an intent only while its immutable generation/fence is current."""
        intent = self._fal_intent(intent_id)
        subject = self._fal_subject(intent.subject_id)
        if intent.generation != generation or subject.generation != generation:
            raise StorageRepositoryError("FAL intent generation is stale")
        if intent.submission_fence != submission_fence:
            raise StorageRepositoryError("FAL submission fence is stale")
        if state not in _FAL_INTENT_STATES:
            raise StorageRepositoryError("FAL intent state is invalid")
        if intent.state in {"completed", "failed", "ambiguous", "canceled"} and intent.state != state:
            raise StorageRepositoryError("terminal FAL intent is immutable")
        if intent.provider_request_id is not None and provider_request_id not in {None, intent.provider_request_id}:
            raise StorageRepositoryError("FAL provider request identity is immutable")
        if provider_request_id is not None:
            if not isinstance(provider_request_id, str) or not provider_request_id or len(provider_request_id) > 500:
                raise StorageRepositoryError("FAL provider request identity is invalid")
            intent.provider_request_id = provider_request_id
        intent.state = state
        if state in {"completed", "failed", "ambiguous", "canceled"}:
            subject.state = "completed" if state == "completed" else state
        self.session.flush()
        return intent

    def create_fal_artifact_receipt(
        self,
        *,
        subject_id: str,
        intent_id: str,
        ordinal: int,
        artifact_identity: Mapping[str, Any],
        metadata_digest: str,
        canonical_list_digest: str,
        stage_fence: str,
        observed_size: int | None = None,
        observed_sha256: str | None = None,
        state: Literal["planned", "observed", "committed", "failed", "quarantined"] = "observed",
        transfer_id: str | None = None,
        eval_output_id: str | None = None,
        generation: int | None = None,
        submission_fence: str | None = None,
    ) -> models.FalArtifactReceipt:
        """Persist one artifact observation linked to its exact submission intent."""
        subject = self._fal_subject(subject_id)
        intent = self._fal_intent(intent_id)
        if intent.subject_id != subject.id:
            raise StorageRepositoryError("FAL artifact intent does not belong to subject")
        if intent.state not in _FAL_INTENT_STATES:
            raise StorageRepositoryError("FAL intent is not receiptable")
        if generation is not None and generation != intent.generation:
            raise StorageRepositoryError("FAL artifact generation is stale")
        if subject.generation != intent.generation:
            raise StorageRepositoryError("FAL subject generation is stale")
        if submission_fence is not None and submission_fence != intent.submission_fence:
            raise StorageRepositoryError("FAL submission fence is stale")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 0 <= ordinal < subject.expected_output_count:
            raise StorageRepositoryError("FAL artifact ordinal is outside the admitted count")
        if not isinstance(artifact_identity, Mapping) or not artifact_identity:
            raise StorageRepositoryError("FAL artifact identity is required")
        if self._contains_locator(artifact_identity):
            raise StorageRepositoryError("FAL artifact identity cannot contain a locator")
        if "provider_artifact_id" in artifact_identity and (
            not isinstance(artifact_identity["provider_artifact_id"], str)
            or not artifact_identity["provider_artifact_id"]
        ):
            raise StorageRepositoryError("FAL provider artifact identity is invalid")
        identity = dict(artifact_identity)
        metadata = self._digest(metadata_digest)
        canonical = self._digest(canonical_list_digest)
        if not isinstance(stage_fence, str) or not stage_fence:
            raise StorageRepositoryError("FAL artifact stage fence is required")
        if state not in _FAL_ARTIFACT_STATES:
            raise StorageRepositoryError("FAL artifact state is invalid")
        if observed_size is not None and (
            not isinstance(observed_size, int) or isinstance(observed_size, bool) or observed_size < 0
        ):
            raise StorageRepositoryError("FAL observed size is invalid")
        observed_digest = self._digest(observed_sha256) if observed_sha256 is not None else None
        if transfer_id is not None:
            self._transfer(transfer_id)
        if eval_output_id is not None:
            self._eval_output(eval_output_id)
        existing = self.session.scalar(
            select(models.FalArtifactReceipt).where(
                models.FalArtifactReceipt.subject_id == subject.id,
                models.FalArtifactReceipt.intent_id == intent.id,
                models.FalArtifactReceipt.ordinal == ordinal,
            )
        )
        if existing is not None:
            immutable = (
                existing.artifact_identity == identity
                and existing.metadata_digest == metadata
                and existing.canonical_list_digest == canonical
                and existing.stage_fence == stage_fence
                and existing.observed_size == observed_size
                and existing.observed_sha256 == observed_digest
                and existing.transfer_id == transfer_id
                and existing.eval_output_id == eval_output_id
            )
            if not immutable:
                raise StorageRepositoryError("FAL artifact receipt is immutable")
            return existing
        receipt = models.FalArtifactReceipt(
            subject_id=subject.id,
            intent_id=intent.id,
            ordinal=ordinal,
            artifact_identity=identity,
            metadata_digest=metadata,
            canonical_list_digest=canonical,
            state=state,
            observed_size=observed_size,
            observed_sha256=observed_digest,
            stage_fence=stage_fence,
            transfer_id=transfer_id,
            eval_output_id=eval_output_id,
        )
        self.session.add(receipt)
        self.session.flush()
        return receipt

    def create_fal_artifact_receipts(
        self,
        *,
        intent_id: str,
        artifacts: Sequence[Mapping[str, Any]],
        canonical_list_digest: str,
        stage_fence: str,
        generation: int | None = None,
        submission_fence: str | None = None,
    ) -> list[models.FalArtifactReceipt]:
        """Persist a canonical batch of receipt rows without accepting locators."""
        intent = self._fal_intent(intent_id)
        subject = self._fal_subject(intent.subject_id)
        rows = list(artifacts)
        if len(rows) != subject.expected_output_count:
            raise StorageRepositoryError("FAL artifact count does not match subject expectation")
        result: list[models.FalArtifactReceipt] = []
        for item in rows:
            if not isinstance(item, Mapping):
                raise StorageRepositoryError("FAL artifact must be an object")
            if "ordinal" not in item or "artifact_identity" not in item or "metadata_digest" not in item:
                raise StorageRepositoryError("FAL artifact receipt fields are required")
            result.append(
                self.create_fal_artifact_receipt(
                    subject_id=subject.id,
                    intent_id=intent.id,
                    ordinal=item["ordinal"],
                    artifact_identity=item["artifact_identity"],
                    metadata_digest=item["metadata_digest"],
                    canonical_list_digest=canonical_list_digest,
                    stage_fence=stage_fence,
                    observed_size=item.get("observed_size"),
                    observed_sha256=item.get("observed_sha256"),
                    state=item.get("state", "observed"),
                    transfer_id=item.get("transfer_id"),
                    eval_output_id=item.get("eval_output_id"),
                    generation=generation,
                    submission_fence=submission_fence,
                )
            )
        return result


    def _new_verified_location(
        self,
        *,
        transfer: models.StorageTransfer,
        asset: models.Asset,
        receipt: models.PublicationReceipt,
        local_root_id: str | None,
        relative_path: str | None,
        source_id: str | None,
        object_key: str | None,
    ) -> models.AssetLocation:
        if transfer.destination_provider == "local":
            root_id = local_root_id or transfer.destination_root_id
            path = self._safe_relative_path(relative_path or "")
            root = self._root(self._required(root_id, "local root"))
            if root.id != transfer.destination_root_id:
                raise PublicationCommitError("publication root differs from transfer destination")
            location = models.AssetLocation(
                asset_id=asset.id,
                workspace_id=self.workspace_id,
                provider="local",
                uri=f"local://{root.id}/{path}",
                local_root_id=root.id,
                relative_path=path,
                size=receipt.size,
                verified_size=receipt.size,
                verified_sha256=receipt.sha256,
                verification_state="available",
                last_verified_at=receipt.verified_at,
                hydration_state="hydrated",
            )
        else:
            resolved_source = source_id or transfer.destination_source_id
            key = self._safe_relative_path(object_key or "")
            source = self._source(self._required(resolved_source, "S3 source"))
            if source.id != transfer.destination_source_id:
                raise PublicationCommitError("publication source differs from transfer destination")
            location = models.AssetLocation(
                asset_id=asset.id,
                workspace_id=self.workspace_id,
                provider="s3",
                uri=f"s3://{source.id}/{key}",
                bucket=source.bucket,
                object_key=key,
                etag=receipt.etag,
                version_id=receipt.version_id,
                source_id=source.id,
                source_revision_fingerprint=source.identity_fingerprint,
                size=receipt.size,
                verified_size=receipt.size,
                verified_sha256=receipt.sha256,
                verification_state="available",
                last_verified_at=receipt.verified_at,
                hydration_state="remote",
            )
        self.session.add(location)
        self.session.flush()
        return location

    def _validate_current_receipt(
        self,
        transfer: models.StorageTransfer,
        attempt: models.PublicationAttempt,
        receipt: models.PublicationReceipt,
    ) -> None:
        if transfer.state != "verified":
            raise PublicationCommitError("transfer is not verified under its active commit fence")
        if attempt.generation != transfer.generation:
            raise PublicationCommitError("publication attempt generation is stale")
        if receipt.publication_fence != attempt.publication_fence:
            raise PublicationCommitError("publication fence is stale")
        if attempt.publication_fence != transfer.commit_fence:
            raise PublicationCommitError("publication fence does not match the active commit fence")
        if receipt.commit_fence != transfer.commit_fence:
            raise PublicationCommitError("commit fence is stale")
        if receipt.sha256 != transfer.expected_sha256 or receipt.size != transfer.expected_size:
            raise PublicationCommitError("receipt does not match transfer expectation")

    def _validate_migration_owner(self, transfer: models.StorageTransfer) -> None:
        if transfer.migration_id is None:
            return
        owner = self.session.scalar(
            select(models.MigrationExecutionOwner).where(
                models.MigrationExecutionOwner.workspace_id == self.workspace_id,
                models.MigrationExecutionOwner.parent_id == transfer.migration_id,
            )
        )
        if owner is None or owner.owner_epoch != transfer.migration_owner_epoch:
            raise PublicationCommitError("migration owner fence is stale")

    def _root(self, root_id: str) -> models.LocalRoot:
        root = self.session.scalar(select(models.LocalRoot).where(models.LocalRoot.id == root_id, models.LocalRoot.workspace_id == self.workspace_id))
        if root is None:
            raise StorageNotFound("local root")
        return root

    def _source(self, source_id: str) -> models.ImportSource:
        source = self.session.scalar(select(models.ImportSource).where(models.ImportSource.id == source_id, models.ImportSource.workspace_id == self.workspace_id))
        if source is None:
            raise StorageNotFound("source")
        return source

    def _snapshot(self, snapshot_id: str) -> models.StoragePolicySnapshot:
        snapshot = self.session.scalar(select(models.StoragePolicySnapshot).where(models.StoragePolicySnapshot.id == snapshot_id, models.StoragePolicySnapshot.workspace_id == self.workspace_id))
        if snapshot is None:
            raise StorageNotFound("storage policy snapshot")
        return snapshot
    def _checkpoint_revision(self, revision_id: str) -> models.CheckpointRevision:
        revision = self.session.scalar(
            select(models.CheckpointRevision)
            .join(models.Checkpoint, models.CheckpointRevision.checkpoint_id == models.Checkpoint.id)
            .join(models.Asset, models.CheckpointRevision.asset_id == models.Asset.id)
            .where(
                models.CheckpointRevision.id == revision_id,
                models.Asset.workspace_id == self.workspace_id,
            )
        )
        if revision is None:
            raise StorageNotFound("checkpoint revision")
        return revision

    def _eval_run(self, eval_run_id: str) -> models.EvalRun:
        run = self.session.scalar(
            select(models.EvalRun)
            .join(models.EvalDefinition, models.EvalRun.definition_id == models.EvalDefinition.id)
            .join(models.Project, models.EvalDefinition.project_id == models.Project.id)
            .where(models.EvalRun.id == eval_run_id, models.Project.workspace_id == self.workspace_id)
        )
        if run is None:
            raise StorageNotFound("eval run")
        return run

    def _eval_output(self, eval_output_id: str) -> models.EvalOutput:
        output = self.session.scalar(
            select(models.EvalOutput)
            .join(models.EvalRun, models.EvalOutput.eval_run_id == models.EvalRun.id)
            .join(models.EvalDefinition, models.EvalRun.definition_id == models.EvalDefinition.id)
            .join(models.Project, models.EvalDefinition.project_id == models.Project.id)
            .where(models.EvalOutput.id == eval_output_id, models.Project.workspace_id == self.workspace_id)
        )
        if output is None:
            raise StorageNotFound("eval output")
        return output

    def _fal_admission(self, admission_id: str) -> models.FalAdmission:
        admission = self.session.scalar(
            select(models.FalAdmission).where(
                models.FalAdmission.id == admission_id,
                models.FalAdmission.workspace_id == self.workspace_id,
            )
        )
        if admission is None:
            raise StorageNotFound("FAL admission")
        return admission

    def _fal_subject(self, subject_id: str) -> models.FalSubject:
        subject = self.session.scalar(
            select(models.FalSubject)
            .join(models.FalAdmission, models.FalSubject.admission_id == models.FalAdmission.id)
            .where(
                models.FalSubject.id == subject_id,
                models.FalAdmission.workspace_id == self.workspace_id,
            )
        )
        if subject is None:
            raise StorageNotFound("FAL subject")
        return subject

    def _fal_intent(self, intent_id: str) -> models.FalSubmissionIntent:
        intent = self.session.scalar(
            select(models.FalSubmissionIntent)
            .join(models.FalSubject, models.FalSubmissionIntent.subject_id == models.FalSubject.id)
            .join(models.FalAdmission, models.FalSubject.admission_id == models.FalAdmission.id)
            .where(
                models.FalSubmissionIntent.id == intent_id,
                models.FalAdmission.workspace_id == self.workspace_id,
            )
        )
        if intent is None:
            raise StorageNotFound("FAL submission intent")
        return intent


    def _migration(self, migration_id: str) -> models.StorageMigration:
        migration = self.session.scalar(select(models.StorageMigration).where(models.StorageMigration.id == migration_id, models.StorageMigration.workspace_id == self.workspace_id))
        if migration is None:
            raise StorageNotFound("storage migration")
        return migration

    def _migration_preview(self, preview_id: str) -> models.StorageMigrationPreview:
        preview = self.session.scalar(
            select(models.StorageMigrationPreview).where(
                models.StorageMigrationPreview.id == preview_id,
                models.StorageMigrationPreview.workspace_id == self.workspace_id,
            )
        )
        if preview is None:
            raise StorageNotFound("storage migration preview")
        return preview

    def _validate_destination(
        self,
        provider: Literal["local", "s3"],
        root_id: str | None,
        source_id: str | None,
    ) -> None:
        if provider == "local":
            if root_id is None or source_id is not None:
                raise StorageRepositoryError("local destination requires one local root")
            self._root(root_id)
        elif provider == "s3":
            if source_id is None or root_id is not None:
                raise StorageRepositoryError("S3 destination requires one S3 source")
            self._source(source_id)
        else:
            raise StorageRepositoryError("destination provider must be local or s3")

    def _location(self, location_id: str) -> models.AssetLocation:
        location = self.session.scalar(select(models.AssetLocation).where(models.AssetLocation.id == location_id, models.AssetLocation.workspace_id == self.workspace_id))
        if location is None:
            raise StorageNotFound("asset location")
        return location

    def _transfer(self, transfer_id: str) -> models.StorageTransfer:
        transfer = self.session.scalar(select(models.StorageTransfer).where(models.StorageTransfer.id == transfer_id, models.StorageTransfer.workspace_id == self.workspace_id))
        if transfer is None:
            raise StorageNotFound("storage transfer")
        return transfer

    def _attempt(self, attempt_id: str) -> models.PublicationAttempt:
        attempt = self.session.scalar(
            select(models.PublicationAttempt)
            .join(models.StorageTransfer, models.PublicationAttempt.transfer_id == models.StorageTransfer.id)
            .where(models.PublicationAttempt.id == attempt_id, models.StorageTransfer.workspace_id == self.workspace_id)
        )
        if attempt is None:
            raise StorageNotFound("publication attempt")
        return attempt
    @staticmethod
    def _digest(value: str) -> str:
        if not isinstance(value, str):
            raise StorageRepositoryError("SHA-256 must be a lowercase hexadecimal digest")
        normalized = value.lower()
        if not _SHA256.fullmatch(normalized):
            raise StorageRepositoryError("SHA-256 must be a lowercase hexadecimal digest")
        return normalized

    @staticmethod
    def _contains_locator(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if isinstance(key, str) and key.lower() in {"url", "download_url", "path", "uri"}:
                    return True
                if StorageRepository._contains_locator(child):
                    return True
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(StorageRepository._contains_locator(child) for child in value)
        return False

    @staticmethod
    def _required(value: str | None, name: str) -> str:
        if value is None:
            raise PublicationCommitError(f"{name} is required")
        return value

    @staticmethod
    def _safe_relative_path(value: str) -> str:
        if not _SAFE_RELATIVE_PATH.fullmatch(value):
            raise PublicationCommitError("location path must be a safe relative path")
        return value

    @staticmethod
    def _preferred_provider(asset: models.Asset, locations: list[models.AssetLocation]) -> str:
        preferred = next((location for location in locations if location.id == asset.preferred_location_id), None)
        return preferred.provider if preferred is not None else locations[0].provider
