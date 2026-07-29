from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class WorkspaceOut(ORMModel):
    id: str
    name: str
    created_at: datetime


class WorkspaceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProfileCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    email: str | None = None
    initials: str | None = Field(default=None, max_length=8)
    avatar_color: str | None = None


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: str | None = None
    initials: str | None = Field(default=None, max_length=8)
    avatar_color: str | None = None
    is_active: bool | None = None


class ProfileOut(ORMModel):
    id: str
    workspace_id: str
    display_name: str
    email: str | None
    initials: str | None
    avatar_color: str | None
    is_active: bool
    created_at: datetime


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str | None = None
    trigger_words: list[str] = []


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    trigger_words: list[str] | None = None
    state: Literal["active", "archived"] | None = None


class ProjectOut(ORMModel):
    id: str
    workspace_id: str
    title: str
    description: str | None
    trigger_words: list[str]
    state: str
    created_at: datetime
    updated_at: datetime


class AssetCreate(BaseModel):
    project_id: str | None = None
    kind: Literal["image", "model", "config", "manifest", "caption", "log", "other"] = "other"
    name: str = Field(min_length=1, max_length=500)
    mime_type: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    metadata: dict[str, Any] = {}
    location: AssetLocationCreate | None = None


class AssetLocationCreate(BaseModel):
    provider: Literal["local", "s3"]
    uri: str
    bucket: str | None = None
    object_key: str | None = None
    etag: str | None = None
    size: int | None = Field(default=None, ge=0)
    hydration_state: Literal["remote", "cached", "hydrated", "remote_missing"] = "remote"


class AssetLocationOut(ORMModel):
    id: str
    provider: str
    uri: str
    bucket: str | None
    object_key: str | None
    etag: str | None
    size: int | None
    hydration_state: str


class AssetOut(ORMModel):
    id: str
    project_id: str | None
    kind: str
    name: str
    mime_type: str | None
    sha256: str | None
    metadata_: dict[str, Any]
    locations: list[AssetLocationOut] = []
    created_at: datetime

    @computed_field
    @property
    def asset_revision_id(self) -> str:
        return self.id

class GalleryEvidence(str, Enum):
    explicit = "explicit"
    canonical = "canonical"
    eval_sample = "eval-sample"
    dataset_membership = "dataset-membership"
    deterministic_import = "deterministic-import"
    unknown = "unknown"


class GalleryEntityRef(BaseModel):
    id: str | None = None
    name: str | None = None
    evidence: GalleryEvidence = GalleryEvidence.unknown


class GalleryMetadataOut(BaseModel):
    origin_type: str = "ASSET"
    evidence: dict[str, GalleryEvidence] = Field(default_factory=dict)
    relationships: dict[str, GalleryEntityRef] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow")


class DatasetCreate(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=240)
    description: str | None = None
    source_uri: str | None = None
    caption_format: Literal["text", "json"] = "text"
    trigger_words: list[str] = []
    items: list[DatasetItemCreate] = []


class DatasetItemCreate(BaseModel):
    asset_id: str
    caption: str = ""
    caption_format: Literal["text", "json"] = "text"
    included: bool = True
    tags: list[str] = []
    position: int = 0

class DatasetDuplicateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    description: str | None = None
    open_draft: bool = False



class DraftCreate(BaseModel):
    base_version_id: str


class DraftItemPatch(BaseModel):
    caption: str | None = None
    caption_format: Literal["text", "json"] | None = None
    included: bool | None = None
    tags: list[str] | None = None


class CaptionOperationRequest(BaseModel):
    operation: Literal["replace", "add_word", "remove_word", "set_included"]
    parameters: dict[str, Any] = {}
    item_ids: list[str] | None = None
    tag: str | None = None
    included: bool | None = None
    caption_format: str | None = None
    all: bool = False
    preview_token: str | None = None

    @model_validator(mode="after")
    def validate_parameters(self):
        required = {"replace": ["find", "replace"], "add_word": ["word"], "remove_word": ["word"], "set_included": ["included"]}
        missing = [key for key in required[self.operation] if key not in self.parameters]
        if missing:
            raise ValueError(f"missing parameters: {', '.join(missing)}")
        if not (self.item_ids or self.tag is not None or self.included is not None or self.caption_format is not None or self.all):
            raise ValueError("explicit scope required: item_ids, tag, included, caption_format, or all=true")
        if self.item_ids is not None and len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("item_ids must be unique")
        if self.item_ids and self.all:
            raise ValueError("scope must use item_ids or all=true, not both")
        return self



class CaptionGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    item_ids: list[str] | None = None
    all: bool = False
    caption_format: Literal["text", "json"] = "text"
    model: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_scope(self):
        if bool(self.item_ids) == self.all:
            raise ValueError("explicit scope required: item_ids or all=true")
        if self.item_ids is not None and len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("item_ids must be unique")
        return self


class CaptionFormatUpdate(BaseModel):
    caption_format: Literal["text", "json"]

class DraftPublish(BaseModel):
    name: str = Field(min_length=1, max_length=240)


class DatasetSubsetCreate(BaseModel):
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=240)
    description: str | None = None
    role: Literal["style", "subject", "source", "quality", "holdout", "custom"] = "custom"
    parent_subset_id: str | None = None
    color_token: Literal["blue", "cyan", "teal", "green", "mint", "grey"] = "blue"
    position: int = Field(default=0, ge=0)


class DatasetSubsetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    role: Literal["style", "subject", "source", "quality", "holdout", "custom"] | None = None
    parent_subset_id: str | None = None
    color_token: Literal["blue", "cyan", "teal", "green", "mint", "grey"] | None = None
    position: int | None = Field(default=None, ge=0)


class DatasetSubsetAssignment(BaseModel):
    subset_id: str
    draft_item_ids: list[str] = Field(min_length=1)
    membership_role: Literal["primary", "secondary"] = "primary"


class WandbCredentialCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=120)
    public_key: str = Field(min_length=40, max_length=128)
    project_id: str | None = None


class WandbCredentialRotate(BaseModel):
    public_key: str = Field(min_length=40, max_length=128)


class TrainingLaunchCreate(BaseModel):
    dataset_version_id: str
    wandb_credential_id: str
    source_id: str
    client_request_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=240)
    trainer: Literal["ai-toolkit"] = "ai-toolkit"
    base_model: str = Field(min_length=1, max_length=500)
    output_directory: str = Field(min_length=1, max_length=2048)
    checkpoint_policy: dict[str, Any] = Field(default_factory=dict)
    backup_policy: dict[str, Any] = Field(default_factory=dict)
    supported_endpoint_ids: list[str] = Field(default_factory=list)
    expected_duration_seconds: int = Field(default=604800, ge=300, le=2592000)
    live_telemetry: bool = False


class RunCreate(BaseModel):
    project_id: str
    dataset_version_id: str | None = None
    name: str
    trainer: str | None = None
    base_model: str | None = None
    status: str = "unknown"
    source_prefix: str | None = None
    raw_manifest: dict[str, Any] = {}
    raw_state: dict[str, Any] = {}
    normalized_config: dict[str, Any] = {}


class TrainingRunDatasetInputCreate(BaseModel):
    dataset_version_id: str
    subset_id: str | None = None
    alias: str | None = Field(default=None, max_length=120)
    item_count_snapshot: int = Field(default=0, ge=0)
    sampling_weight: float = Field(default=1.0, gt=0)
    repeat_count: int = Field(default=1, ge=1)
    position: int = Field(default=0, ge=0)
    dataset_content_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class TrainingRunDatasetInputUpdate(BaseModel):
    alias: str | None = Field(default=None, max_length=120)
    sampling_weight: float | None = Field(default=None, gt=0)
    repeat_count: int | None = Field(default=None, ge=1)
    position: int | None = Field(default=None, ge=0)


class MergeRecipeInput(BaseModel):
    alias: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
    checkpoint_revision_id: str
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    step: int | None = Field(default=None, ge=0)
    rank: int | None = Field(default=None, ge=1)
    role: Literal["general", "specialist", "donor", "primary", "custom"] = "custom"
    key_format: Literal["native", "ai_toolkit"] = "native"
    weights: dict[str, float]

    @model_validator(mode="after")
    def validate_weights(self):
        if not self.weights:
            raise ValueError("merge input weights cannot be empty")
        for scope, value in self.weights.items():
            if scope not in {"global", "text_fusion", "transformer"} and not scope.startswith("module:"):
                raise ValueError(f"unsupported merge weight scope: {scope}")
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"merge weight for {scope} must be between 0 and 1")
        return self


class MergeRecipeOutput(BaseModel):
    rank: int = Field(ge=1)
    dtype: Literal["float32", "float16", "bfloat16"] = "float16"


class MergeRecipe(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_version: Literal["modelfiche.checkpoint-merge/v1"] = Field(
        default="modelfiche.checkpoint-merge/v1",
        alias="$schema",
    )
    name: str = Field(min_length=1, max_length=240)
    operator: Literal["linear_weighted_sum", "layer_weighted_sum", "rank_concat"]
    base_model: str = Field(min_length=1, max_length=500)
    inputs: list[MergeRecipeInput] = Field(min_length=2)
    output: MergeRecipeOutput

    @model_validator(mode="after")
    def validate_recipe(self):
        aliases = [item.alias for item in self.inputs]
        if len(aliases) != len(set(aliases)):
            raise ValueError("merge input aliases must be unique")
        scopes = {scope for item in self.inputs for scope in item.weights}
        for scope in scopes:
            total = sum(item.weights.get(scope, 0.0) for item in self.inputs)
            if abs(total - 1.0) > 1e-6:
                raise ValueError(f"merge weights for {scope} must sum to 1.0, got {total}")
        if self.operator == "linear_weighted_sum" and scopes != {"global"}:
            raise ValueError("linear_weighted_sum requires only global weights")
        if self.operator in {"layer_weighted_sum", "rank_concat"} and not {"text_fusion", "transformer"}.issubset(scopes):
            raise ValueError(f"{self.operator} requires text_fusion and transformer weights")
        return self


class MergeRegistration(BaseModel):
    model_id: str | None = None
    version_name: str | None = Field(default=None, min_length=1, max_length=240)
    trigger_words: list[str] = Field(default_factory=list)
    notes: str | None = None
    lifecycle_state: Literal["candidate"] = "candidate"

    @model_validator(mode="after")
    def validate_model_version(self):
        if self.model_id and not self.version_name:
            raise ValueError("version_name is required when model_id is supplied")
        return self


class MergePrepareRequest(BaseModel):
    project_id: str
    client_request_id: str = Field(min_length=1, max_length=128)
    recipe: MergeRecipe
    registration: MergeRegistration = Field(default_factory=MergeRegistration)


class MergeCompleteRequest(BaseModel):
    output_path: str = Field(min_length=1, max_length=2048)
    output_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    output_size: int = Field(ge=1)
    output_step: int = Field(default=0, ge=0)


class MergeFailRequest(BaseModel):
    phase: str = Field(min_length=1, max_length=80)
    error_code: str = Field(min_length=1, max_length=80)
    error_message: str = Field(min_length=1)


class MergeHeartbeatRequest(BaseModel):
    phase: str | None = Field(default=None, max_length=80)


class CheckpointCreate(BaseModel):
    step: int = Field(ge=0)
    asset_id: str
    stage_id: str | None = None
    state: str = "available"


class SampleCreate(BaseModel):
    asset_id: str
    checkpoint_id: str | None = None
    step: int | None = Field(default=None, ge=0)
    prompt: str | None = None
    seed: int | None = None
    generation_metadata: dict[str, Any] = {}


class ModelCreate(BaseModel):
    project_id: str
    name: str
    description: str | None = None


class ModelVersionCreate(BaseModel):
    model_id: str
    checkpoint_id: str
    name: str
    trigger_words: list[str] = []
    base_model: str | None = None
    notes: str | None = None
    lifecycle_state: Literal["candidate", "approved", "archived"] = "candidate"


class ModelVersionState(BaseModel):
    lifecycle_state: Literal["candidate", "approved", "archived"]


class SubjectCreate(BaseModel):
    subject_type: str = Field(min_length=1, max_length=64)
    subject_id: str


class ReviewCreate(SubjectCreate):
    profile_id: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    decision: Literal["candidate", "approved", "hold", "reject"] | None = None


class CommentCreate(SubjectCreate):
    profile_id: str | None = None
    body: str = Field(min_length=1)


class NoteCreate(CommentCreate):
    supersedes_id: str | None = None


class LineageCreate(BaseModel):
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    relationship: str
    metadata: dict[str, Any] = {}


class JobCreate(BaseModel):
    kind: str
    profile_id: str | None = None
    idempotency_key: str | None = None
    payload: dict[str, Any] = {}






class InlinePromptCreate(BaseModel):
    id: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1, max_length=64_000)
    position: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)




class AxisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    values: list[Any] = Field(min_length=1)


class PlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: str = "2026-07-22.v1"
    axes: dict[str, AxisRequest | None]
    cases: list[dict[str, Any]] = Field(min_length=1)
    targets: list[dict[str, Any]] = Field(min_length=1)
    fixed_case_id: str | None = None
    fixed_target_id: str | None = None
    shared_params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_axes(self):
        if set(self.axes) - {"x", "y", "z"} or "x" not in self.axes or "y" not in self.axes or self.axes.get("x") is None or self.axes.get("y") is None:
            raise ValueError("axes must contain non-null x and y and may contain z")
        return self


class PlanPreflightRequest(PlanPayload):
    idempotency_key: str = Field(min_length=1, max_length=200)


class GridCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=240)
    idempotency_key: str = Field(min_length=1, max_length=200)
    plan: PlanPayload

class GridQueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=200)
    billing_acknowledgement: Literal["I understand FAL may bill this run even if checkpoint fetch fails"]
    plan_version: int = Field(ge=1)
    plan_digest: str = Field(min_length=8, max_length=80)
    cell_ordinals: list[int] | None = None




class EvalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=240)
    idempotency_key: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1)
    assessment: dict[str, Any] = Field(default_factory=dict)
