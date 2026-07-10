from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UUIDMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))


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


class Workspace(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"
    name: Mapped[str] = mapped_column(String(200))


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
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    locations: Mapped[list[AssetLocation]] = relationship(back_populates="asset", cascade="all, delete-orphan")


class AssetLocation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "asset_locations"
    __table_args__ = (UniqueConstraint("provider", "bucket", "object_key", "etag"),)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    uri: Mapped[str] = mapped_column(String(2048))
    bucket: Mapped[str | None] = mapped_column(String(255))
    object_key: Mapped[str | None] = mapped_column(String(1024))
    etag: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int | None] = mapped_column(Integer)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hydration_state: Mapped[str] = mapped_column(String(32), default="remote")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    asset: Mapped[Asset] = relationship(back_populates="locations")


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


class TrainingRun(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "training_runs"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset_version_id: Mapped[str | None] = mapped_column(ForeignKey("dataset_versions.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(240))
    trainer: Mapped[str | None] = mapped_column(String(100))
    base_model: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    source_prefix: Mapped[str | None] = mapped_column(String(1024))
    raw_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    normalized_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TrainingStage(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "training_stages"
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Checkpoint(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "checkpoints"
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    stage_id: Mapped[str | None] = mapped_column(ForeignKey("training_stages.id", ondelete="SET NULL"))
    step: Mapped[int] = mapped_column(Integer, index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    state: Mapped[str] = mapped_column(String(32), default="available")


class Sample(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "samples"
    run_id: Mapped[str] = mapped_column(ForeignKey("training_runs.id", ondelete="CASCADE"), index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoints.id", ondelete="SET NULL"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    step: Mapped[int | None] = mapped_column(Integer, index=True)
    prompt: Mapped[str | None] = mapped_column(Text)
    seed: Mapped[int | None] = mapped_column(Integer)
    generation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


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


class EvalDefinition(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "eval_definitions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    endpoint: Mapped[str] = mapped_column(String(240))
    model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id", ondelete="SET NULL"))
    prompt_set_id: Mapped[str] = mapped_column(ForeignKey("prompt_sets.id", ondelete="RESTRICT"))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EvalRun(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "eval_runs"
    definition_id: Mapped[str] = mapped_column(ForeignKey("eval_definitions.id", ondelete="CASCADE"), index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(ForeignKey("checkpoints.id", ondelete="SET NULL"), index=True)
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


class GridDefinition(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "grid_definitions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240))
    eval_definition_id: Mapped[str] = mapped_column(ForeignKey("eval_definitions.id", ondelete="CASCADE"))
    x_axis: Mapped[dict[str, Any]] = mapped_column(JSON)
    y_axis: Mapped[dict[str, Any]] = mapped_column(JSON)


class GridCell(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "grid_cells"
    grid_definition_id: Mapped[str] = mapped_column(ForeignKey("grid_definitions.id", ondelete="CASCADE"), index=True)
    eval_output_id: Mapped[str] = mapped_column(ForeignKey("eval_outputs.id", ondelete="CASCADE"))
    x_index: Mapped[int] = mapped_column(Integer)
    y_index: Mapped[int] = mapped_column(Integer)


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
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("user_profiles.id", ondelete="RESTRICT"), index=True)
    rating: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[str | None] = mapped_column(String(32))


class Comment(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "comments"
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
