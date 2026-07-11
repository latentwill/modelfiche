from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class StorageContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContentVariantV1(StorageContractModel):
    kind: Literal["content"]


class ThumbnailVariantV1(StorageContractModel):
    kind: Literal["thumbnail"]
    max_pixels: Literal[256, 512, 1024]


AssetDeliveryVariantV1: TypeAlias = Annotated[
    ContentVariantV1 | ThumbnailVariantV1,
    Field(discriminator="kind"),
]


class AssetDeliveryRequestV1(StorageContractModel):
    asset_revision_id: UUID
    variant: AssetDeliveryVariantV1


class AlternateDeliveryRequestV1(StorageContractModel):
    alternate_token: str = Field(min_length=1)
    diagnostic_token: str = Field(min_length=1)
    asset_revision_id: UUID
    variant: AssetDeliveryVariantV1


class DiagnoseDeliveryRequestV1(StorageContractModel):
    diagnostic_token: str = Field(min_length=1)


class ReacquireDescriptorActionV1(StorageContractModel):
    kind: Literal["reacquire_descriptor_once"]


class ConsumeAlternateActionV1(StorageContractModel):
    kind: Literal["consume_alternate_once"]
    alternate_token: str = Field(min_length=1)


class OpenStorageDetailsActionV1(StorageContractModel):
    kind: Literal["open_storage_details"]
    href: str = Field(pattern=r"^#/image/[0-9a-fA-F-]{36}$")
    focus_target_id: str = Field(min_length=1)


class UserRetryActionV1(StorageContractModel):
    kind: Literal["user_retry_once"]


class StopDeliveryActionV1(StorageContractModel):
    kind: Literal["stop"]


DeliveryNextActionV1: TypeAlias = Annotated[
    ReacquireDescriptorActionV1
    | ConsumeAlternateActionV1
    | OpenStorageDetailsActionV1
    | UserRetryActionV1
    | StopDeliveryActionV1,
    Field(discriminator="kind"),
]


class DeliveryFailureV1(StorageContractModel):
    code: Literal[
        "capability_expired",
        "primary_failed_alternate_available",
        "storage_configuration_required",
        "delivery_capacity",
        "native_delivery_failed",
        "alternate_failed",
    ]
    redacted_message: str = Field(min_length=1)
    retryable: bool
    action: DeliveryNextActionV1


class AssetDeliveryDescriptorV1(StorageContractModel):
    version: Literal[1] = 1
    diagnostic_token: str = Field(min_length=1)
    asset_revision_id: UUID
    selected_location_id: UUID
    provider: Literal["local", "s3"]
    source_label: str = Field(min_length=1)
    variant: AssetDeliveryVariantV1
    delivered_mime_type: str = Field(min_length=1)
    delivered_size_bytes: int | None = Field(default=None, ge=0)
    mode: Literal["direct_s3", "proxy"]
    delivery_url: str = Field(min_length=1)
    expires_at: datetime
    fallback_used: bool
    alternate_token: str | None = None
    summary_id: str = Field(min_length=1)


class DescriptorDeliveryResultV1(StorageContractModel):
    kind: Literal["descriptor"]
    descriptor: AssetDeliveryDescriptorV1


class FailureDeliveryResultV1(StorageContractModel):
    kind: Literal["failure"]
    failure: DeliveryFailureV1


AssetDeliveryResultV1: TypeAlias = Annotated[
    DescriptorDeliveryResultV1 | FailureDeliveryResultV1,
    Field(discriminator="kind"),
]


class DiagnoseDeliveryResultV1(StorageContractModel):
    kind: Literal["failure"] = "failure"
    failure: DeliveryFailureV1


class AnnouncementV1(StorageContractModel):
    event_id: str = Field(min_length=1)
    mode: Literal["polite", "assertive"]
    message: str
    focus_target_id: str | None = None


class CancelMigrationActionV1(StorageContractModel):
    action: Literal["cancel"]
    expected_version: int = Field(ge=0)


class ResumeRemainingMigrationActionV1(StorageContractModel):
    action: Literal["resume_remaining"]
    expected_version: int = Field(ge=0)


class RetryFailedMigrationActionV1(StorageContractModel):
    action: Literal["retry_failed"]
    expected_version: int = Field(ge=0)


class ContinueIncompleteMigrationActionV1(StorageContractModel):
    action: Literal["continue_incomplete"]
    expected_version: int = Field(ge=0)


class ReviewChangedMigrationActionV1(StorageContractModel):
    action: Literal["review_changed"]
    expected_version: int = Field(ge=0)


class ReplaceDestinationMigrationActionV1(StorageContractModel):
    action: Literal["replace_destination"]
    expected_version: int = Field(ge=0)
    preview_id: UUID
    preview_version: int = Field(ge=0)


class AbandonRemainingMigrationActionV1(StorageContractModel):
    action: Literal["abandon_remaining"]
    expected_version: int = Field(ge=0)


StorageMigrationActionRequestV1: TypeAlias = Annotated[
    CancelMigrationActionV1
    | ResumeRemainingMigrationActionV1
    | RetryFailedMigrationActionV1
    | ContinueIncompleteMigrationActionV1
    | ReviewChangedMigrationActionV1
    | ReplaceDestinationMigrationActionV1
    | AbandonRemainingMigrationActionV1,
    Field(discriminator="action"),
]

_MIGRATION_ACTION_ADAPTER = TypeAdapter(StorageMigrationActionRequestV1)


def parse_migration_action(value: Any) -> StorageMigrationActionRequestV1:
    return _MIGRATION_ACTION_ADAPTER.validate_python(value)


class StorageMigrationScopeAllV1(StorageContractModel):
    kind: Literal["all_images"]


class StorageMigrationScopeProjectV1(StorageContractModel):
    kind: Literal["project"]
    project_id: UUID


class StorageMigrationScopeUnassignedV1(StorageContractModel):
    kind: Literal["unassigned"]


StorageMigrationScopeV1: TypeAlias = Annotated[
    StorageMigrationScopeAllV1 | StorageMigrationScopeProjectV1 | StorageMigrationScopeUnassignedV1,
    Field(discriminator="kind"),
]


class LocalStorageDestinationV1(StorageContractModel):
    kind: Literal["local"]
    root_id: UUID


class S3StorageDestinationV1(StorageContractModel):
    kind: Literal["s3"]
    source_id: UUID


StorageDestinationV1: TypeAlias = Annotated[
    LocalStorageDestinationV1 | S3StorageDestinationV1,
    Field(discriminator="kind"),
]


class StorageMigrationFrozenFieldsV1(StorageContractModel):
    workspace_id: UUID
    scope: StorageMigrationScopeV1
    destination: StorageDestinationV1
    source_snapshot_ids: list[UUID]
    source_inventory_digest: str = Field(min_length=1)
    ordered_asset_digest: str = Field(min_length=1)
    policy_version: int = Field(ge=0)
    created_at: datetime


class StorageMigrationCountsV1(StorageContractModel):
    eligible_items: int = Field(ge=0)
    excluded_items: int = Field(ge=0)
    terminal_items: int = Field(ge=0)
    committed_items: int = Field(ge=0)
    failed_items: int = Field(ge=0)
    remaining_items: int = Field(ge=0)
    unknown_size_items: int = Field(ge=0)
    known_bytes_total: int = Field(ge=0)
    committed_bytes: int = Field(ge=0)
    inflight_bytes: int = Field(ge=0)
    worst_case_bytes: int = Field(ge=0)


class StorageMigrationPreviewReasonV1(StorageContractModel):
    code: Literal[
        "no_eligible_images",
        "destination_not_ready",
        "capacity",
        "source_unavailable",
        "replacement_conflict",
    ]
    redacted_label: str
    affected_items: int = Field(ge=0)


class StorageMigrationPreviewV1(StorageContractModel):
    id: UUID
    version: int = Field(ge=0)
    expires_at: datetime
    frozen_fields: StorageMigrationFrozenFieldsV1
    counts: StorageMigrationCountsV1
    readiness: Literal["ready", "blocked"]
    reasons: list[StorageMigrationPreviewReasonV1]
    admit_enabled: bool
    disabled_reason_code: str | None = None
    replaces_parent_id: UUID | None = None
    expected_parent_version: int | None = None
    expected_owner_epoch: int | None = None
    focus_target_id: str


class StorageMigrationPreviewRequestV1(StorageContractModel):
    scope: StorageMigrationScopeV1
    destination: StorageDestinationV1
    replacement_parent_id: UUID | None = None
    expected_parent_version: int | None = Field(default=None, ge=0)


class StorageMigrationAdmitRequestV1(StorageContractModel):
    preview_id: UUID
    expected_preview_version: int = Field(ge=0)


class StorageMigrationHistoryV1(StorageContractModel):
    sequence: int = Field(ge=0)
    at: datetime
    event_code: Literal[
        "admitted",
        "started",
        "item_committed",
        "item_failed",
        "cancel_requested",
        "capacity_blocked",
        "continued",
        "abandoned",
        "superseded",
        "completed",
    ]
    redacted_summary: str


class StorageMigrationControlV1(StorageContractModel):
    action: Literal[
        "cancel",
        "resume_remaining",
        "retry_failed",
        "continue_incomplete",
        "review_changed",
        "replace_destination",
        "abandon_remaining",
    ]
    label: str
    enabled: bool
    disabled_reason_code: Literal[
        "capacity",
        "source_changed",
        "destination_unavailable",
        "transfer_failed",
        "operator_canceled",
        "storage_contract_mismatch",
    ] | None = None


class StorageMigrationReplacementLinkV1(StorageContractModel):
    relation: Literal["predecessor", "successor"]
    migration_id: UUID
    route: str = Field(pattern=r"^#/transfers/migrations/[0-9a-fA-F-]{36}$")


StorageMigrationStateV1: TypeAlias = Literal[
    "queued",
    "running",
    "cancel_requested",
    "partial",
    "retryable",
    "capacity_blocked",
    "canceled_with_remaining",
    "completed",
    "failed",
    "abandoned",
    "superseded_destination",
    "superseded_source_changed",
]


StorageMigrationReasonCodeV1: TypeAlias = Literal[
    "capacity",
    "source_changed",
    "destination_unavailable",
    "transfer_failed",
    "operator_canceled",
    "storage_contract_mismatch",
]


class StorageMigrationDetailV1(StorageContractModel):
    id: UUID
    version: int = Field(ge=0)
    canonical_route: str = Field(pattern=r"^#/transfers/migrations/[0-9a-fA-F-]{36}$")
    state: StorageMigrationStateV1
    reason_code: StorageMigrationReasonCodeV1 | None = None
    frozen_fields: StorageMigrationFrozenFieldsV1
    counts: StorageMigrationCountsV1
    progress: dict[str, int]
    history: list[StorageMigrationHistoryV1]
    controls: list[StorageMigrationControlV1]
    replacement_links: list[StorageMigrationReplacementLinkV1]
    focus_target_id: str





class MigrationDetailActionResultV1(StorageContractModel):
    kind: Literal["detail"]
    detail: StorageMigrationDetailV1
    announcement: AnnouncementV1


class MigrationPreviewActionResultV1(StorageContractModel):
    kind: Literal["preview"]
    preview: StorageMigrationPreviewV1
    destination_route: str
    focus_target_id: str
    announcement: AnnouncementV1


class MigrationConflictActionResultV1(StorageContractModel):
    kind: Literal["conflict"]
    current: StorageMigrationDetailV1
    winner_route: str
    focus_target_id: str
    announcement: AnnouncementV1


StorageMigrationActionResultV1: TypeAlias = Annotated[
    MigrationDetailActionResultV1 | MigrationPreviewActionResultV1 | MigrationConflictActionResultV1,
    Field(discriminator="kind"),
]


class StoragePolicyCandidateV1(StorageContractModel):
    source_id: UUID
    label: str
    state: Literal["pending", "failed", "read_only", "write_ready"]
    capabilities: list[str]
    blocked_reason: str | None = None


class StoragePolicyV1(StorageContractModel):
    version: int = Field(ge=0)
    desired_provider: Literal["local", "s3"]
    resolved_provider: Literal["local", "s3"]
    selection_origin: Literal["automatic", "operator"]
    write_source_id: UUID | None = None
    fal_local_fallback: bool
    blocked_reason: str | None = None
    candidates: list[StoragePolicyCandidateV1]


class StoragePolicyPatchV1(StorageContractModel):
    expected_version: int = Field(ge=0)
    desired_provider: Literal["local", "s3"]
    write_source_id: UUID | None = None
    fal_local_fallback: bool = True
