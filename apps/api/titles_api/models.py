from __future__ import annotations

import enum
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC and restore tzinfo on SQLite, whose DATETIME is naive."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        utc = aware.astimezone(timezone.utc)
        return utc.replace(tzinfo=None) if dialect.name == "sqlite" else utc

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class UUIDMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    def __init__(self, **kwargs: Any) -> None:
        if kwargs.get("id") is None:
            kwargs["id"] = str(uuid.uuid4())
        super().__init__(**kwargs)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProjectState(str, enum.Enum):
    active = "active"
    archived = "archived"


class AssetKind(str, enum.Enum):
    image = "image"
    model = "model"
    config = "config"
    manifest = "manifest"
    caption = "caption"
    log = "log"
    other = "other"


class JobState(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"

def workspace_slug(name: str) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:80] or "workspace"
    )


def _default_workspace_slug(context) -> str:
    return workspace_slug(
        str(context.get_current_parameters().get("name") or "workspace")
    )


class Workspace(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(
        String(80), unique=True, index=True, default=_default_workspace_slug
    )


class UserProfile(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "user_profiles"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    initials: Mapped[str | None] = mapped_column(String(8))
    avatar_color: Mapped[str | None] = mapped_column(String(24))
    email: Mapped[str | None] = mapped_column(String(320))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class LocalPreference(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "local_preferences"
    key: Mapped[str] = mapped_column(String(100), unique=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Project(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    workspace: Mapped[Workspace] = relationship("Workspace")
    title: Mapped[str] = mapped_column(String(240), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    state: Mapped[ProjectState] = mapped_column(Enum(ProjectState), default=ProjectState.active)
    trigger_words: Mapped[list[str]] = mapped_column(JSON, default=list)


class Asset(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "assets"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    kind: Mapped[AssetKind] = mapped_column(Enum(AssetKind), default=AssetKind.other, index=True)
    name: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str | None] = mapped_column(String(200))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    content_blob_id: Mapped[str | None] = mapped_column(ForeignKey("content_blobs.id", ondelete="SET NULL"), index=True)
    preferred_location_id: Mapped[str | None] = mapped_column(ForeignKey("asset_locations.id", ondelete="SET NULL"), index=True)
    origin_location_id: Mapped[str | None] = mapped_column(ForeignKey("asset_locations.id", ondelete="SET NULL"), index=True)
    provenance_kind: Mapped[str] = mapped_column(String(32), default="legacy", index=True)
    generated_artifact_receipt_id: Mapped[str | None] = mapped_column(ForeignKey("fal_artifact_receipts.id", ondelete="SET NULL"), index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    locations: Mapped[list[AssetLocation]] = relationship(back_populates="asset", cascade="all, delete-orphan", foreign_keys="AssetLocation.asset_id")


class AssetLocation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "asset_locations"
    __table_args__ = (
        Index(
            "uq_asset_locations_source_revision",
            "asset_id",
            "source_id",
            "object_key",
            "source_revision_fingerprint",
            unique=True,
            sqlite_where=text("source_id IS NOT NULL AND source_revision_fingerprint IS NOT NULL"),
            postgresql_where=text("source_id IS NOT NULL AND source_revision_fingerprint IS NOT NULL"),
        ),
    )
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    uri: Mapped[str] = mapped_column(String(2048))
    bucket: Mapped[str | None] = mapped_column(String(255))
    object_key: Mapped[str | None] = mapped_column(String(1024))
    etag: Mapped[str | None] = mapped_column(String(255))
    version_id: Mapped[str | None] = mapped_column(String(1024))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    source_revision_fingerprint: Mapped[str | None] = mapped_column(String(96), index=True)
    local_root_id: Mapped[str | None] = mapped_column(ForeignKey("local_roots.id", ondelete="RESTRICT"), index=True)
    relative_path: Mapped[str | None] = mapped_column(String(2048))
    size: Mapped[int | None] = mapped_column(Integer)
    verified_size: Mapped[int | None] = mapped_column(Integer)
    verified_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    verification_state: Mapped[str] = mapped_column(String(32), default="unverified", index=True)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hydration_state: Mapped[str] = mapped_column(String(32), default="remote")
    repair_attribution: Mapped[str | None] = mapped_column(String(128))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    asset: Mapped[Asset] = relationship(back_populates="locations", foreign_keys=[asset_id])


class ImageMetadata(UUIDMixin, Base):
    __tablename__ = "image_metadata"
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), unique=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    color_mode: Mapped[str | None] = mapped_column(String(32))
    orientation: Mapped[int | None] = mapped_column(Integer)
    exif_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    perceptual_hash: Mapped[str | None] = mapped_column(String(128), index=True)


class Dataset(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "datasets"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    description: Mapped[str | None] = mapped_column(Text)


class DatasetVersion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version_number"),)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(240))
    source_uri: Mapped[str | None] = mapped_column(String(2048))
    caption_format: Mapped[str] = mapped_column(String(32), default="text")
    trigger_words: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="published")
    published_by_profile_id: Mapped[str | None] = mapped_column(ForeignKey("user_profiles.id", ondelete="SET NULL"))
    parent_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id", ondelete="SET NULL"), index=True)
    lineage_kind: Mapped[str] = mapped_column(String(32), default="initial", index=True)
    content_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    items: Mapped[list[DatasetItem]] = relationship(cascade="all, delete-orphan")


class DatasetItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_items"
    __table_args__ = (UniqueConstraint("dataset_version_id", "asset_id"),)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), index=True)
    caption: Mapped[str] = mapped_column(Text, default="")
    caption_format: Mapped[str] = mapped_column(String(32), default="text")
    included: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    position: Mapped[int] = mapped_column(Integer, default=0)


class DatasetVersionSubset(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_version_subsets"
    __table_args__ = (UniqueConstraint("dataset_version_id", "key", name="uq_dataset_version_subset_key"),)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(240))
    description: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32), default="custom", index=True)
    parent_subset_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_version_subsets.id", ondelete="SET NULL"), index=True)
    color_token: Mapped[str] = mapped_column(String(32), default="blue")
    position: Mapped[int] = mapped_column(Integer, default=0)


class DatasetSubsetItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_subset_items"
    __table_args__ = (UniqueConstraint("subset_id", "dataset_item_id", name="uq_dataset_subset_item"),)
    subset_id: Mapped[str] = mapped_column(ForeignKey("dataset_version_subsets.id", ondelete="CASCADE"), index=True)
    dataset_item_id: Mapped[str] = mapped_column(ForeignKey("dataset_items.id", ondelete="CASCADE"), index=True)
    membership_role: Mapped[str] = mapped_column(String(32), default="primary", index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class DatasetDraft(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_drafts"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    base_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"))
    created_by_profile_id: Mapped[str | None] = mapped_column(ForeignKey("user_profiles.id", ondelete="SET NULL"))
    state: Mapped[str] = mapped_column(String(32), default="active")
    items: Mapped[list[DatasetDraftItem]] = relationship(cascade="all, delete-orphan")


class DatasetDraftItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_draft_items"
    __table_args__ = (UniqueConstraint("draft_id", "source_item_id"),)
    draft_id: Mapped[str] = mapped_column(ForeignKey("dataset_drafts.id", ondelete="CASCADE"), index=True)
    source_item_id: Mapped[str] = mapped_column(ForeignKey("dataset_items.id", ondelete="CASCADE"))
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), index=True)
    caption: Mapped[str] = mapped_column(Text, default="")
    caption_format: Mapped[str] = mapped_column(String(32), default="text")
    included: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    position: Mapped[int] = mapped_column(Integer, default=0)


class DatasetDraftSubset(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_draft_subsets"
    __table_args__ = (UniqueConstraint("draft_id", "key", name="uq_dataset_draft_subset_key"),)
    draft_id: Mapped[str] = mapped_column(ForeignKey("dataset_drafts.id", ondelete="CASCADE"), index=True)
    source_subset_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_version_subsets.id", ondelete="SET NULL"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(240))
    description: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32), default="custom", index=True)
    parent_subset_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_draft_subsets.id", ondelete="SET NULL"), index=True)
    color_token: Mapped[str] = mapped_column(String(32), default="blue")
    position: Mapped[int] = mapped_column(Integer, default=0)


class DatasetDraftSubsetItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "dataset_draft_subset_items"
    __table_args__ = (UniqueConstraint("subset_id", "draft_item_id", name="uq_dataset_draft_subset_item"),)
    subset_id: Mapped[str] = mapped_column(ForeignKey("dataset_draft_subsets.id", ondelete="CASCADE"), index=True)
    draft_item_id: Mapped[str] = mapped_column(ForeignKey("dataset_draft_items.id", ondelete="CASCADE"), index=True)
    membership_role: Mapped[str] = mapped_column(String(32), default="primary", index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)


class CaptionOperation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "caption_operations"
    draft_id: Mapped[str] = mapped_column(ForeignKey("dataset_drafts.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("user_profiles.id", ondelete="SET NULL"))
    operation: Mapped[str] = mapped_column(String(32))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    before_count: Mapped[int] = mapped_column(Integer, default=0)
    after_count: Mapped[int] = mapped_column(Integer, default=0)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class WandbIngestCredential(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "wandb_ingest_credentials"
    alias: Mapped[str] = mapped_column(String(120), unique=True)
    algorithm: Mapped[str] = mapped_column(String(32), default="ed25519")
    key_id: Mapped[str] = mapped_column(String(64), index=True)
    public_key: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TrainingLaunch(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "training_launches"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_training_launch_run"),
        UniqueConstraint("workspace_id", "client_request_id", name="uq_training_launch_client_request"),
    )
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id", ondelete="RESTRICT"), index=True)
    wandb_credential_id: Mapped[str] = mapped_column(ForeignKey("wandb_ingest_credentials.id", ondelete="RESTRICT"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="preparing", index=True)
    client_request_id: Mapped[str] = mapped_column(String(128))
    manifest_version: Mapped[str] = mapped_column(String(64), default="modelfiche.training-launch.v1")
    redacted_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    manifest_digest: Mapped[str | None] = mapped_column(String(64), index=True)
    dataset_export_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    dataset_export_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String(96))
    run_prefix: Mapped[str] = mapped_column(String(1024))
    backup_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checkpoint_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    supported_endpoint_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    credential_bound_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    credential_validated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    backup_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    checkpoint_handoff_status: Mapped[str] = mapped_column(String(32), default="awaiting_manifest", index=True)
    next_reconcile_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    last_reconcile_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reconcile_generation: Mapped[int] = mapped_column(Integer, default=0)
    reconcile_error: Mapped[str | None] = mapped_column(Text)


class TrainingRunPlacement(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "training_run_placements"
    __table_args__ = (UniqueConstraint("run_id", "epoch", name="uq_training_run_placement_epoch"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), index=True)
    previous_workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id", ondelete="RESTRICT"))
    previous_project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    reason: Mapped[str | None] = mapped_column(Text)
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("user_profiles.id", ondelete="SET NULL"))


class TrainingRun(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "training_runs"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(240))
    trainer: Mapped[str | None] = mapped_column(String(100))
    run_kind: Mapped[str] = mapped_column(String(32), default="training", index=True)
    base_model: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    source_prefix: Mapped[str | None] = mapped_column(String(1024))
    origin_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    origin_source_fingerprint: Mapped[str | None] = mapped_column(String(96))
    raw_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    normalized_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    wandb_run_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    wandb_project: Mapped[str | None] = mapped_column(String(240), index=True)
    wandb_display_name: Mapped[str | None] = mapped_column(String(240))
    wandb_sdk_version: Mapped[str | None] = mapped_column(String(32))
    wandb_protocol_revision: Mapped[str | None] = mapped_column(String(64))
    current_step: Mapped[int | None] = mapped_column(Integer, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    placement_epoch: Mapped[int] = mapped_column(Integer, default=1)


class TrainingStage(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "training_stages"
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TrainingRunDatasetInput(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "training_run_dataset_inputs"
    __table_args__ = (
        UniqueConstraint("run_id", "dataset_version_id", "subset_id", name="uq_training_run_dataset_input"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    dataset_version_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.id", ondelete="RESTRICT"), index=True)
    subset_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_version_subsets.id", ondelete="RESTRICT"), index=True)
    alias: Mapped[str | None] = mapped_column(String(120))
    item_count_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    sampling_weight: Mapped[float] = mapped_column(Float, default=1.0)
    repeat_count: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer, default=0)
    dataset_content_digest: Mapped[str | None] = mapped_column(String(64), index=True)

class TrainingMetric(UUIDMixin, Base):
    __tablename__ = "training_metrics"
    __table_args__ = (
        UniqueConstraint("run_id", "step", "name"),
        Index("ix_training_metrics_run_name_step", "run_id", "name", "step"),
        CheckConstraint(
            "(value_type = 'number' AND value IS NOT NULL AND value_text IS NULL) OR "
            "(value_type = 'text' AND value IS NULL AND value_text IS NOT NULL) OR "
            "(value_type = 'null' AND value IS NULL AND value_text IS NULL)",
            name="ck_training_metric_typed_value",
        ),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    value: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(16), default="number")
    wall_time: Mapped[float | None] = mapped_column(Float)
    source_key: Mapped[str | None] = mapped_column(String(1024))
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    batch_key: Mapped[str | None] = mapped_column(String(128), index=True)

class RunMetricSummary(UUIDMixin, Base):
    __tablename__ = "run_metric_summaries"
    __table_args__ = (
        UniqueConstraint("run_id", "name", name="uq_run_metric_summary"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    last_step: Mapped[int] = mapped_column(Integer)
    last_value: Mapped[float | None] = mapped_column(Float)
    last_value_text: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(16))
    minimum: Mapped[float | None] = mapped_column(Float)
    maximum: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RunEvent(UUIDMixin, Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
        UniqueConstraint("idempotency_key", name="uq_run_event_idempotency"),
        Index("ix_run_events_run_sequence", "run_id", "sequence"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(64), index=True)
    step: Mapped[int | None] = mapped_column(Integer, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class RunUpload(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "run_uploads"
    __table_args__ = (
        UniqueConstraint("run_id", "relative_path", name="uq_run_upload_path"),
        Index("ix_run_uploads_run_state", "run_id", "state"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    relative_path: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    metric_name: Mapped[str | None] = mapped_column(String(120))
    step: Mapped[int | None] = mapped_column(Integer, index=True)
    caption: Mapped[str | None] = mapped_column(Text)
    expected_sha256: Mapped[str | None] = mapped_column(String(64))
    expected_size: Mapped[int | None] = mapped_column(Integer)
    mime_type: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    presign_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    etag: Mapped[str | None] = mapped_column(String(255))
    version_id: Mapped[str | None] = mapped_column(String(1024))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class RunArtifactHandoff(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "run_artifact_handoffs"
    __table_args__ = (
        UniqueConstraint("run_id", "generation", name="uq_run_artifact_handoff_generation"),
        UniqueConstraint("run_id", "manifest_version_id", name="uq_run_artifact_handoff_manifest_version"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    manifest_key: Mapped[str] = mapped_column(String(1024))
    manifest_etag: Mapped[str | None] = mapped_column(String(255))
    manifest_version_id: Mapped[str | None] = mapped_column(String(1024))
    manifest_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="observed", index=True)
    source_fingerprint: Mapped[str] = mapped_column(String(96))
    checkpoint_count: Mapped[int] = mapped_column(Integer, default=0)
    verified_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    imported_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Checkpoint(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "checkpoints"
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    stage_id: Mapped[str | None] = mapped_column(ForeignKey("training_stages.id", ondelete="SET NULL"))
    step: Mapped[int] = mapped_column(Integer, index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    state: Mapped[str] = mapped_column(String(32), default="available")
    current_revision_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoint_revisions.id", ondelete="SET NULL"), index=True)


class Sample(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "samples"
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoints.id", ondelete="SET NULL"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    step: Mapped[int | None] = mapped_column(Integer, index=True)
    prompt: Mapped[str | None] = mapped_column(Text)
    seed: Mapped[int | None] = mapped_column(Integer)
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    modified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)


class Model(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "models"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    description: Mapped[str | None] = mapped_column(Text)


class ModelVersion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "model_versions"
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"), index=True)
    checkpoint_id: Mapped[str] = mapped_column(ForeignKey("checkpoints.id", ondelete="RESTRICT"), unique=True)
    name: Mapped[str] = mapped_column(String(240))
    trigger_words: Mapped[list[str]] = mapped_column(JSON, default=list)
    base_model: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    lifecycle_state: Mapped[str] = mapped_column(String(32), default="candidate", index=True)
    artifact_type: Mapped[str] = mapped_column(String(32), default="lora", index=True)
    artifact_format: Mapped[str | None] = mapped_column(String(120))
    method: Mapped[str | None] = mapped_column(String(120))
    compatibility: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checkpoint_revision_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoint_revisions.id", ondelete="SET NULL"), index=True)
    readiness: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PromptSet(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "prompt_sets"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    version: Mapped[int] = mapped_column(Integer, default=1)
    prompts: Mapped[list[Prompt]] = relationship(cascade="all, delete-orphan")


class Prompt(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "prompts"
    prompt_set_id: Mapped[str] = mapped_column(ForeignKey("prompt_sets.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class ExperimentPlan(UUIDMixin, TimestampMixin, Base):
    """Immutable, project-owned generation plan shared by grids and evals."""
    __tablename__ = "experiment_plans"
    __table_args__ = (
        UniqueConstraint("project_id", "digest", name="uq_experiment_plan_project_digest"),
        UniqueConstraint("project_id", "idempotency_key", name="uq_experiment_plan_project_idempotency"),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    contract_version: Mapped[str] = mapped_column(String(32), default="2026-07-22.v1")
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    digest: Mapped[str] = mapped_column(String(80), index=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))


class EvalDefinition(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "eval_definitions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    endpoint: Mapped[str] = mapped_column(String(240), default="")
    model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id", ondelete="SET NULL"))
    prompt_set_id: Mapped[str | None] = mapped_column(ForeignKey("prompt_sets.id", ondelete="RESTRICT"))
    inline_prompts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("experiment_plans.id", ondelete="SET NULL"), index=True)
    plan_version: Mapped[int | None] = mapped_column(Integer)
    plan_digest: Mapped[str | None] = mapped_column(String(80), index=True)

class EvalRun(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "eval_runs"
    definition_id: Mapped[str] = mapped_column(ForeignKey("eval_definitions.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("experiment_plans.id", ondelete="SET NULL"), index=True)
    plan_version: Mapped[int | None] = mapped_column(Integer)
    plan_digest: Mapped[str | None] = mapped_column(String(80), index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoints.id", ondelete="SET NULL"), index=True)
    checkpoint_revision_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoint_revisions.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    provider_job_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    parameters_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EvalOutput(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "eval_outputs"
    eval_run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"), index=True)
    prompt_id: Mapped[str | None] = mapped_column(ForeignKey("prompts.id", ondelete="SET NULL"))
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    seed: Mapped[int | None] = mapped_column(Integer)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)


class EvalAssessment(UUIDMixin, TimestampMixin, Base):
    """Assessment only; generation configuration lives in the referenced plan/run."""
    __tablename__ = "eval_assessments"
    __table_args__ = (UniqueConstraint("project_id", "idempotency_key", name="uq_eval_assessment_project_idempotency"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("experiment_plans.id", ondelete="RESTRICT"), index=True)
    plan_version: Mapped[int] = mapped_column(Integer)
    plan_digest: Mapped[str] = mapped_column(String(80), index=True)
    assessment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="ready_for_review", index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))


class GridDefinition(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "grid_definitions"
    __table_args__ = (UniqueConstraint("project_id", "idempotency_key", name="uq_grid_definition_project_idempotency"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    eval_definition_id: Mapped[str | None] = mapped_column(ForeignKey("eval_definitions.id", ondelete="CASCADE"))
    x_axis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    y_axis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    z_axis: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("experiment_plans.id", ondelete="RESTRICT"), index=True)
    plan_version: Mapped[int | None] = mapped_column(Integer)
    plan_digest: Mapped[str | None] = mapped_column(String(80), index=True)
    plan_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[str] = mapped_column(String(32), default="0.000000")
    idempotency_key: Mapped[str | None] = mapped_column(String(200))


class GridCell(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "grid_cells"
    __table_args__ = (
        UniqueConstraint("grid_definition_id", "ordinal", name="uq_grid_cell_ordinal"),
        UniqueConstraint("grid_definition_id", "x_index", "y_index", "z_index", name="uq_grid_cell_coordinate"),
    )
    grid_definition_id: Mapped[str] = mapped_column(ForeignKey("grid_definitions.id", ondelete="CASCADE"), index=True)
    eval_output_id: Mapped[str | None] = mapped_column(ForeignKey("eval_outputs.id", ondelete="SET NULL"))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    x_index: Mapped[int] = mapped_column(Integer)
    y_index: Mapped[int] = mapped_column(Integer)
    z_index: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")
    coordinate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    case_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    target_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    endpoint_id: Mapped[str] = mapped_column(String(240), default="")
    endpoint_adapter: Mapped[str] = mapped_column(String(64), default="fal")
    schema_digest: Mapped[str] = mapped_column(String(80), default="")
    effective_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    output_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    request_count: Mapped[int] = mapped_column(Integer, default=1)
    estimated_cost: Mapped[str] = mapped_column(String(32), default="0.000000")
    idempotency_key: Mapped[str | None] = mapped_column(String(200))





class ImportSource(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "import_sources"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    provider: Mapped[str] = mapped_column(String(32), default="s3")
    endpoint_url: Mapped[str | None] = mapped_column(String(2048))
    region: Mapped[str | None] = mapped_column(String(100))
    addressing_style: Mapped[str] = mapped_column(String(16), default="auto")
    credential_env_prefix: Mapped[str] = mapped_column(String(40), default="S3")
    bucket: Mapped[str] = mapped_column(String(255))
    allowed_prefixes: Mapped[list[str]] = mapped_column(JSON, default=list)
    identity_fingerprint: Mapped[str | None] = mapped_column(String(96), index=True)
    managed_prefix: Mapped[str] = mapped_column(String(1024), default="titles-dam/managed/")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ImportJob(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "import_jobs"
    source_id: Mapped[str] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), unique=True)
    prefix: Mapped[str] = mapped_column(String(1024))
    detected_type: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProviderJob(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "provider_jobs"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(500), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Review(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "reviews"
    __table_args__ = (
        Index("ix_reviews_subject_created_at", "subject_type", "subject_id", "created_at"),
    )
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("user_profiles.id", ondelete="RESTRICT"), index=True)
    rating: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[str | None] = mapped_column(String(32))


class Comment(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "comments"
    __table_args__ = (
        Index("ix_comments_subject_created_at", "subject_type", "subject_id", "created_at"),
    )
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("user_profiles.id", ondelete="RESTRICT"), index=True)
    body: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Note(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "notes"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("user_profiles.id", ondelete="RESTRICT"))
    body: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("notes.id", ondelete="SET NULL"))


class LineageEdge(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "lineage_edges"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    relationship: Mapped[str] = mapped_column(String(64), index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class Job(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "jobs"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[JobState] = mapped_column(Enum(JobState), default=JobState.queued, index=True)
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("user_profiles.id", ondelete="SET NULL"))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress: Mapped[float] = mapped_column(Float, default=0)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="running", index=True)
    pid: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class ActivityEvent(UUIDMixin, Base):
    __tablename__ = "activity_events"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("user_profiles.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ContentBlob(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "content_blobs"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_content_blob_sha256"),
        CheckConstraint("size >= 0", name="ck_content_blob_nonnegative_size"),
    )
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(200))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LocalRoot(UUIDMixin, Base):
    __tablename__ = "local_roots"
    __table_args__ = (UniqueConstraint("workspace_id", "canonical_path", name="uq_local_root_workspace_path"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    canonical_path: Mapped[str] = mapped_column(String(2048))
    fingerprint: Mapped[dict[str, Any]] = mapped_column(JSON)
    owner_uid: Mapped[int] = mapped_column(Integer)
    mode: Mapped[int] = mapped_column(Integer)
    availability: Mapped[str] = mapped_column(String(32), default="available", index=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceCapability(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "source_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "capability",
            "source_fingerprint",
            "adapter_version",
            "boot_id",
            "credential_epoch",
            name="uq_source_capability_epoch",
        ),
    )
    source_id: Mapped[str] = mapped_column(ForeignKey("import_sources.id", ondelete="CASCADE"), index=True)
    source_fingerprint: Mapped[str] = mapped_column(String(96))
    adapter_version: Mapped[str] = mapped_column(String(64))
    boot_id: Mapped[str] = mapped_column(String(64))
    credential_epoch: Mapped[str] = mapped_column(String(96))
    capability: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    redacted_detail: Mapped[str | None] = mapped_column(Text)


class StoragePolicy(Base):
    __tablename__ = "storage_policies"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    desired_provider: Mapped[str] = mapped_column(String(16), default="local")
    resolved_provider: Mapped[str] = mapped_column(String(16), default="local")
    write_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    selection_origin: Mapped[str] = mapped_column(String(16), default="automatic")
    fal_local_fallback: Mapped[bool] = mapped_column(Boolean, default=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class StoragePolicySnapshot(UUIDMixin, Base):
    __tablename__ = "storage_policy_snapshots"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    contract_version: Mapped[int] = mapped_column(Integer, default=1)
    desired_provider: Mapped[str] = mapped_column(String(16))
    resolved_provider: Mapped[str] = mapped_column(String(16))
    origin_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    origin_source_fingerprint: Mapped[str | None] = mapped_column(String(96))
    write_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    write_source_fingerprint: Mapped[str | None] = mapped_column(String(96))
    write_managed_prefix: Mapped[str | None] = mapped_column("managed_prefix", String(1024))
    local_root_id: Mapped[str | None] = mapped_column(ForeignKey("local_roots.id", ondelete="RESTRICT"), index=True)
    fal_local_fallback: Mapped[bool] = mapped_column(Boolean, default=True)
    operation_id: Mapped[str] = mapped_column(String(128), index=True)
    actor_profile_id: Mapped[str | None] = mapped_column(ForeignKey("user_profiles.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StorageMigration(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "storage_migrations"
    __table_args__ = (
        Index(
            "uq_storage_migration_active_workspace",
            "workspace_id",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running', 'cancel_requested')"),
        ),
    )
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("storage_policy_snapshots.id", ondelete="RESTRICT"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(48), default="queued", index=True)
    scope_kind: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), index=True)
    destination_provider: Mapped[str] = mapped_column(String(16))
    destination_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    destination_root_id: Mapped[str | None] = mapped_column(ForeignKey("local_roots.id", ondelete="RESTRICT"), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    predecessor_id: Mapped[str | None] = mapped_column(ForeignKey("storage_migrations.id", ondelete="SET NULL"), index=True)


class StorageMigrationPreview(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "storage_migration_previews"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32), default="open", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    scope_kind: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), index=True)
    destination_provider: Mapped[str] = mapped_column(String(16))
    destination_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    destination_root_id: Mapped[str | None] = mapped_column(ForeignKey("local_roots.id", ondelete="RESTRICT"), index=True)
    predecessor_id: Mapped[str | None] = mapped_column(ForeignKey("storage_migrations.id", ondelete="SET NULL"), index=True)
    predecessor_version: Mapped[int | None] = mapped_column(Integer)
    expected_owner_epoch: Mapped[int | None] = mapped_column(Integer)
    frozen_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    eligible_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    known_bytes: Mapped[int] = mapped_column(Integer, default=0)
    unknown_bytes: Mapped[int] = mapped_column(Integer, default=0)
    worst_case_bytes: Mapped[int] = mapped_column(Integer, default=0)
    readiness_state: Mapped[str] = mapped_column(String(32), default="ready")
    disabled_reason: Mapped[str | None] = mapped_column(String(128))


class StorageMigrationPreviewItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "storage_migration_preview_items"
    __table_args__ = (UniqueConstraint("preview_id", "ordinal", name="uq_storage_migration_preview_item_ordinal"),)
    preview_id: Mapped[str] = mapped_column(ForeignKey("storage_migration_previews.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), index=True)
    content_blob_id: Mapped[str | None] = mapped_column(ForeignKey("content_blobs.id", ondelete="RESTRICT"), index=True)
    source_location_id: Mapped[str | None] = mapped_column(ForeignKey("asset_locations.id", ondelete="RESTRICT"), index=True)
    selected_revision_fingerprint: Mapped[str | None] = mapped_column(String(96))
    destination_provider: Mapped[str] = mapped_column(String(16))
    destination_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    destination_root_id: Mapped[str | None] = mapped_column(ForeignKey("local_roots.id", ondelete="RESTRICT"), index=True)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    known_size: Mapped[int | None] = mapped_column(Integer)


class MigrationExecutionOwner(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "migration_execution_owners"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_migration_owner_workspace"),
        UniqueConstraint("parent_id", name="uq_migration_owner_parent"),
    )
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[str] = mapped_column(ForeignKey("storage_migrations.id", ondelete="CASCADE"), index=True)
    owner_epoch: Mapped[int] = mapped_column(Integer, default=0)


class StorageTransfer(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "storage_transfers"
    __table_args__ = (
        Index(
            "uq_storage_transfer_active_operation",
            "logical_operation_id",
            "asset_id",
            "destination_provider",
            unique=True,
            sqlite_where=text("state IN ('pending', 'streaming', 'uploaded_unverified', 'verified')"),
        ),
    )
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("storage_policy_snapshots.id", ondelete="RESTRICT"), index=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), index=True)
    source_location_id: Mapped[str | None] = mapped_column(ForeignKey("asset_locations.id", ondelete="RESTRICT"), index=True)
    destination_provider: Mapped[str] = mapped_column(String(16))
    destination_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    destination_root_id: Mapped[str | None] = mapped_column(ForeignKey("local_roots.id", ondelete="RESTRICT"), index=True)
    logical_operation_id: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commit_fence: Mapped[str] = mapped_column(String(128), index=True)
    expected_sha256: Mapped[str] = mapped_column(String(64))
    expected_size: Mapped[int] = mapped_column(Integer)
    migration_id: Mapped[str | None] = mapped_column(ForeignKey("storage_migrations.id", ondelete="SET NULL"), index=True)
    migration_owner_epoch: Mapped[int | None] = mapped_column(Integer)


class StorageReservation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "storage_reservations"
    __table_args__ = (Index("ix_storage_reservation_transfer_state", "transfer_id", "state"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    transfer_id: Mapped[str | None] = mapped_column(ForeignKey("storage_transfers.id", ondelete="CASCADE"), index=True)
    root_id: Mapped[str | None] = mapped_column(ForeignKey("local_roots.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    reserved_bytes: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="active", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublicationAttempt(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "publication_attempts"
    __table_args__ = (UniqueConstraint("transfer_id", "generation", name="uq_publication_attempt_transfer_generation"),)
    transfer_id: Mapped[str] = mapped_column(ForeignKey("storage_transfers.id", ondelete="CASCADE"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    nonce: Mapped[str] = mapped_column(String(128))
    publication_fence: Mapped[str] = mapped_column(String(128), index=True)
    final_locator: Mapped[str] = mapped_column(String(2048))
    expected_sha256: Mapped[str] = mapped_column(String(64))
    expected_size: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)


class PublicationReceipt(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "publication_receipts"
    __table_args__ = (UniqueConstraint("attempt_id", name="uq_publication_receipt_attempt"),)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("publication_attempts.id", ondelete="CASCADE"), index=True)
    provider_locator: Mapped[str] = mapped_column(String(2048))
    version_id: Mapped[str | None] = mapped_column(String(1024))
    etag: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    publication_fence: Mapped[str] = mapped_column(String(128))
    commit_fence: Mapped[str] = mapped_column(String(128))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StorageMigrationItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "storage_migration_items"
    __table_args__ = (UniqueConstraint("migration_id", "ordinal", name="uq_storage_migration_item_ordinal"),)
    migration_id: Mapped[str] = mapped_column(ForeignKey("storage_migrations.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), index=True)
    content_blob_id: Mapped[str | None] = mapped_column(ForeignKey("content_blobs.id", ondelete="RESTRICT"), index=True)
    source_location_id: Mapped[str] = mapped_column(ForeignKey("asset_locations.id", ondelete="RESTRICT"), index=True)
    destination_provider: Mapped[str] = mapped_column(String(16))
    destination_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    destination_root_id: Mapped[str | None] = mapped_column(ForeignKey("local_roots.id", ondelete="RESTRICT"), index=True)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str | None] = mapped_column(String(64), index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)


class CheckpointRevision(UUIDMixin, Base):
    __tablename__ = "checkpoint_revisions"
    __table_args__ = (UniqueConstraint("checkpoint_id", "ordinal", name="uq_checkpoint_revision_ordinal"),)
    checkpoint_id: Mapped[str] = mapped_column(ForeignKey("checkpoints.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), index=True)
    content_blob_id: Mapped[str | None] = mapped_column(ForeignKey("content_blobs.id", ondelete="RESTRICT"), index=True)
    source_location_id: Mapped[str | None] = mapped_column(ForeignKey("asset_locations.id", ondelete="RESTRICT"), index=True)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoint_revisions.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MergeOperation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "merge_operations"
    __table_args__ = (
        UniqueConstraint("project_id", "recipe_digest", name="uq_merge_operation_recipe"),
        UniqueConstraint("run_id", name="uq_merge_operation_run"),
    )
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    schema_version: Mapped[str] = mapped_column(String(80), default="modelfiche.checkpoint-merge/v1")
    operator: Mapped[str] = mapped_column(String(64), index=True)
    base_model: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="preparing", index=True)
    recipe: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recipe_digest: Mapped[str] = mapped_column(String(64), index=True)
    compact_notation: Mapped[str | None] = mapped_column(Text)
    output_rank: Mapped[int] = mapped_column(Integer)
    dtype: Mapped[str] = mapped_column(String(32))
    output_checkpoint_revision_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoint_revisions.id", ondelete="SET NULL"), index=True)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    failure_phase: Mapped[str | None] = mapped_column(String(80))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class MergeInput(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "merge_inputs"
    __table_args__ = (
        UniqueConstraint("merge_operation_id", "alias", name="uq_merge_input_alias"),
        UniqueConstraint("merge_operation_id", "position", name="uq_merge_input_position"),
    )
    merge_operation_id: Mapped[str] = mapped_column(ForeignKey("merge_operations.id", ondelete="CASCADE"), index=True)
    checkpoint_revision_id: Mapped[str] = mapped_column(ForeignKey("checkpoint_revisions.id", ondelete="RESTRICT"), index=True)
    alias: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(32), default="custom")
    source_rank: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)


class MergeInputWeight(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "merge_input_weights"
    __table_args__ = (
        UniqueConstraint("merge_input_id", "scope", "module_pattern", name="uq_merge_input_weight_scope"),
    )
    merge_input_id: Mapped[str] = mapped_column(ForeignKey("merge_inputs.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(64), index=True)
    module_pattern: Mapped[str | None] = mapped_column(String(500))
    weight: Mapped[float] = mapped_column(Float)


class FalAdmission(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "fal_admissions"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    eval_run_id: Mapped[str | None] = mapped_column(ForeignKey("eval_runs.id", ondelete="SET NULL"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("storage_policy_snapshots.id", ondelete="RESTRICT"), index=True)
    checkpoint_revision_id: Mapped[str] = mapped_column(ForeignKey("checkpoint_revisions.id", ondelete="RESTRICT"), index=True)
    staging_source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id", ondelete="RESTRICT"), index=True)
    endpoint_id: Mapped[str] = mapped_column(String(240))
    adapter_version: Mapped[str] = mapped_column(String(80), default="v1")
    version: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32), default="unsubmitted", index=True)
    handoff_assurance: Mapped[str] = mapped_column(String(32))
    billing_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_artifact_count: Mapped[int] = mapped_column(Integer)
    spool_root_id: Mapped[str] = mapped_column(ForeignKey("local_roots.id", ondelete="RESTRICT"), index=True)
    spool_grant_bytes: Mapped[int] = mapped_column(Integer)
    permanent_reservation_bytes: Mapped[int] = mapped_column(Integer)
    dispatch_fence: Mapped[str] = mapped_column(String(128))


class GenerationQueueItem(UUIDMixin, TimestampMixin, Base):
    """Provider-neutral durable queue record; provider payloads remain adapter-owned."""
    __tablename__ = "generation_queue_items"
    __table_args__ = (UniqueConstraint("workspace_id", "client_request_id", name="uq_generation_queue_client_request"),)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    client_request_id: Mapped[str] = mapped_column(String(36))
    workflow: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    queue_owner: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[str | None] = mapped_column(ForeignKey("models.id", ondelete="SET NULL"), index=True)
    model_version_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    compiled_request_id: Mapped[str] = mapped_column(String(36), index=True)
    admission_id: Mapped[str | None] = mapped_column(ForeignKey("fal_admissions.id", ondelete="SET NULL"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    request_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider_job_id: Mapped[str | None] = mapped_column(String(500), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)


class GenerationQueueChild(UUIDMixin, TimestampMixin, Base):
    """One ordered provider request within a visible workflow queue item."""
    __tablename__ = "generation_queue_children"
    __table_args__ = (
        UniqueConstraint("queue_item_id", "ordinal", name="uq_generation_queue_child_ordinal"),
        UniqueConstraint("queue_item_id", "compiled_request_id", name="uq_generation_queue_child_request"),
    )
    queue_item_id: Mapped[str] = mapped_column(ForeignKey("generation_queue_items.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id", ondelete="SET NULL"), index=True)
    compiled_request_id: Mapped[str] = mapped_column(String(36), index=True)
    admission_id: Mapped[str | None] = mapped_column(ForeignKey("fal_admissions.id", ondelete="SET NULL"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(500), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class FalSubject(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "fal_subjects"
    __table_args__ = (
        UniqueConstraint("admission_id", "ordinal", name="uq_fal_subject_ordinal"),
        UniqueConstraint("admission_id", "subject_key", "generation", name="uq_fal_subject_key_generation"),
    )
    admission_id: Mapped[str] = mapped_column(ForeignKey("fal_admissions.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    subject_key: Mapped[str] = mapped_column(String(256))
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expected_output_count: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)


class FalSubmissionIntent(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "fal_submission_intents"
    __table_args__ = (UniqueConstraint("subject_id", "generation", name="uq_fal_submission_intent_generation"),)
    admission_id: Mapped[str] = mapped_column(ForeignKey("fal_admissions.id", ondelete="CASCADE"), index=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("fal_subjects.id", ondelete="CASCADE"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    request_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(500), index=True)
    submission_fence: Mapped[str] = mapped_column(String(128))


class FalArtifactReceipt(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "fal_artifact_receipts"
    __table_args__ = (UniqueConstraint("subject_id", "intent_id", "ordinal", name="uq_fal_artifact_receipt_ordinal"),)
    subject_id: Mapped[str] = mapped_column(ForeignKey("fal_subjects.id", ondelete="CASCADE"), index=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("fal_submission_intents.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    artifact_identity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_digest: Mapped[str] = mapped_column(String(64))
    canonical_list_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    observed_size: Mapped[int | None] = mapped_column(Integer)
    observed_sha256: Mapped[str | None] = mapped_column(String(64))
    stage_fence: Mapped[str] = mapped_column(String(128))
    transfer_id: Mapped[str | None] = mapped_column(ForeignKey("storage_transfers.id", ondelete="SET NULL"), index=True)
    eval_output_id: Mapped[str | None] = mapped_column(ForeignKey("eval_outputs.id", ondelete="SET NULL"), index=True)
