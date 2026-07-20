from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Annotated, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services import active_profile, apply_generation_prompt_settings, current_workspace, get_or_404, record_activity
from ..storage.fal_admissions import (
    BILLING_ACKNOWLEDGEMENT,
    BILLING_DISCLOSURE,
    FalAdmissionValidationError,
    build_admission_plan,
    build_eval_subjects,
    build_grid_subjects,
)
from ..integrations.fal.adapters import ValidationError, get_adapter, normalize_parameters

router = APIRouter(tags=["fal"])
DB = Annotated[Session, Depends(get_db)]


class FalContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FalPromptV1(FalContractModel):
    prompt_id: str = Field(min_length=1, max_length=240)
    prompt: str = Field(min_length=1, max_length=64_000)


class FalEvalCreateV1(FalContractModel):
    kind: Literal["eval"]
    mode: Literal["single", "prompt_batch"]
    project_id: UUID
    model_version_id: UUID
    checkpoint_revision_id: UUID
    prompts: list[FalPromptV1]
    parameters: dict[str, Any]
    client_request_id: UUID
    definition_id: UUID | None = None


class GenerationContextV1(FalContractModel):
    kind: Literal["project", "model"]
    project_id: UUID
    model_id: UUID | None = None


class GenerationCompileV1(FalContractModel):
    workflow: Literal["image", "grid"]
    provider: str
    context: GenerationContextV1
    model_version_id: UUID
    checkpoint_revision_id: UUID
    prompt: str = Field(min_length=1, max_length=64_000)
    parameters: dict[str, Any]
    client_request_id: UUID


class EvalGenerationCompileV1(FalContractModel):
    workflow: Literal["eval"]
    provider: str
    context: GenerationContextV1
    model_version_id: UUID
    checkpoint_revision_id: UUID
    prompts: list[FalPromptV1] = Field(min_length=1)
    prompt_set_id: UUID | None = None
    parameters: dict[str, Any]
    client_request_id: UUID


class FalGridCreateV1(FalContractModel):
    kind: Literal["grid"]
    project_id: UUID
    model_version_id: UUID
    checkpoint_revision_id: UUID
    grid_definition_id: UUID
    parameters: dict[str, Any]
    client_request_id: UUID


FalCreateAdmissionV1 = Annotated[FalEvalCreateV1 | FalGridCreateV1, Field(discriminator="kind")]


class FalAdmitRequestV1(FalContractModel):
    expected_version: int = Field(ge=0)
    billing_acknowledgement: Literal["I understand FAL may bill this run even if checkpoint fetch fails"] | None = None


class FalSubjectV1(FalContractModel):
    id: UUID
    kind: Literal["single", "prompt", "grid_cell"]
    stable_key: str
    prompt_id: str | None
    grid_cell_id: UUID | None
    input_digest: str
    expected_output_count: int = Field(ge=1)
    admitted_ordinals: list[int]


class FalBlockReasonV1(FalContractModel):
    code: str
    redacted_label: str


class FalControlV1(FalContractModel):
    action: Literal["admit", "cancel", "configure_storage", "repair_checkpoint_source"]
    enabled: bool
    disabled_reason_code: str | None = None


class FalAdmissionV1(FalContractModel):
    id: UUID
    version: int = Field(ge=0)
    kind: Literal["eval", "grid"]
    state: Literal["unsubmitted", "blocked", "ready", "admitted", "canceled", "expired", "superseded"]
    frozen: dict[str, Any]
    subjects: list[FalSubjectV1]
    expected_artifact_count: int = Field(ge=1)
    expected_max_bytes: int = Field(ge=1)
    handoff_assurance: Literal["locally_verified", "provider_verified"]
    billing_disclosure: str | None
    required_acknowledgement: str | None
    block_reasons: list[FalBlockReasonV1]
    recovery_links: list[dict[str, str]]
    controls: list[FalControlV1]
    canonical_route: str
    focus_target_id: str


class FalAnnouncementV1(FalContractModel):
    event_id: str
    mode: Literal["polite", "assertive"]
    message: str
    focus_target_id: str | None = None


class FalProjectionResultV1(FalContractModel):
    kind: Literal["projection"]
    admission: FalAdmissionV1
    announcement: FalAnnouncementV1


class FalAcceptedResultV1(FalContractModel):
    kind: Literal["accepted"]
    admission: FalAdmissionV1
    eval_run_id: UUID
    canonical_route: str
    focus_target_id: str
    announcement: FalAnnouncementV1


class FalConflictResultV1(FalContractModel):
    kind: Literal["conflict"]
    current: FalAdmissionV1
    canonical_route: str
    focus_target_id: str
    announcement: FalAnnouncementV1


FalAdmissionResultV1 = Annotated[
    FalProjectionResultV1 | FalAcceptedResultV1 | FalConflictResultV1,
    Field(discriminator="kind"),
]


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()


def _announcement(admission_id: str, message: str, *, mode: Literal["polite", "assertive"] = "polite") -> FalAnnouncementV1:
    return FalAnnouncementV1(event_id=f"fal-admission:{admission_id}:{mode}", mode=mode, message=message, focus_target_id=f"fal-admission-{admission_id}")


def _snapshot(db: Session, workspace: models.Workspace, profile_id: str | None, operation_id: str) -> tuple[models.StoragePolicySnapshot, models.LocalRoot, models.StoragePolicy]:
    policy = db.scalar(select(models.StoragePolicy).where(models.StoragePolicy.workspace_id == workspace.id))
    if policy is None:
        policy = models.StoragePolicy(workspace_id=workspace.id, version=0, desired_provider="local", resolved_provider="local", fal_local_fallback=True)
        db.add(policy)
        db.flush()
    root = db.scalar(select(models.LocalRoot).where(models.LocalRoot.workspace_id == workspace.id, models.LocalRoot.kind == "spool", models.LocalRoot.availability == "available").order_by(models.LocalRoot.created_at))
    if root is None:
        raise HTTPException(status_code=409, detail="FAL spool storage is not configured")
    source = db.get(models.ImportSource, policy.write_source_id) if policy.write_source_id else None
    snapshot = models.StoragePolicySnapshot(
        workspace_id=workspace.id,
        contract_version=1,
        desired_provider=policy.desired_provider,
        resolved_provider=policy.resolved_provider,
        write_source_id=source.id if source else None,
        write_source_fingerprint=source.identity_fingerprint if source else None,
        write_managed_prefix=source.managed_prefix if source else None,
        local_root_id=root.id,
        fal_local_fallback=policy.fal_local_fallback,
        operation_id=operation_id,
        actor_profile_id=profile_id,
    )
    db.add(snapshot)
    db.flush()
    return snapshot, root, policy


def _grid_cells(grid: models.GridDefinition) -> list[dict[str, Any]]:
    x_values = list(grid.x_axis.get("values") or [])
    y_values = list(grid.y_axis.get("values") or [])
    if not x_values or not y_values:
        raise FalAdmissionValidationError("grid axes require values")
    cells: list[dict[str, Any]] = []
    for y_index, y_value in enumerate(y_values):
        for x_index, x_value in enumerate(x_values):
            cell_id = str(uuid5(NAMESPACE_URL, f"titles-dam:grid:{grid.id}:{x_index}:{y_index}"))
            cells.append({
                "grid_cell_id": cell_id,
                "x_index": x_index,
                "y_index": y_index,
                "axis_digest": _digest({"x": x_value, "y": y_value, "x_index": x_index, "y_index": y_index}),
                "coordinates": {"x": x_value, "y": y_value},
            })
    return cells


def _projection(db: Session, admission: models.FalAdmission, subjects: list[models.FalSubject] | None = None) -> FalAdmissionV1:
    subjects = subjects if subjects is not None else list(db.scalars(select(models.FalSubject).where(models.FalSubject.admission_id == admission.id).order_by(models.FalSubject.ordinal)))
    snapshot = db.get(models.StoragePolicySnapshot, admission.snapshot_id)
    policy = db.scalar(select(models.StoragePolicy).where(models.StoragePolicy.workspace_id == admission.workspace_id))
    revision = db.get(models.CheckpointRevision, admission.checkpoint_revision_id)
    first_definition = dict(subjects[0].definition) if subjects else {}
    frozen = {
        "workspace_id": admission.workspace_id,
        "project_id": first_definition.get("project_id"),
        "model_version_id": first_definition.get("model_version_id"),
        "checkpoint_revision_id": admission.checkpoint_revision_id,
        "endpoint_id": admission.endpoint_id,
        "adapter_version": "fal-adapter-v1",
        "input_digest": first_definition.get("admission_input_digest", ""),
        "normalized_parameters": first_definition.get("normalized_parameters", {}),
        "storage_policy_version": getattr(policy, "version", getattr(snapshot, "contract_version", 1)),
        "spool_root_id": admission.spool_root_id,
    }
    block_reasons: list[FalBlockReasonV1] = []
    if revision is None:
        block_reasons.append(FalBlockReasonV1(code="checkpoint_revision_missing", redacted_label="Checkpoint source repair required"))
    state = admission.state
    if block_reasons and state in {"unsubmitted", "ready"}:
        state = "blocked"
    controls = [
        FalControlV1(action="admit", enabled=state == "ready"),
        FalControlV1(action="cancel", enabled=state in {"unsubmitted", "ready", "blocked"}),
        FalControlV1(action="configure_storage", enabled=bool(block_reasons), disabled_reason_code=None if block_reasons else "not_needed"),
        FalControlV1(action="repair_checkpoint_source", enabled=any(reason.code == "checkpoint_revision_missing" for reason in block_reasons), disabled_reason_code=None if any(reason.code == "checkpoint_revision_missing" for reason in block_reasons) else "not_needed"),
    ]
    return FalAdmissionV1(
        id=UUID(admission.id),
        version=admission.version,
        kind=str(first_definition.get("admission_kind", "eval")),
        state=state,
        frozen=frozen,
        subjects=[FalSubjectV1(id=UUID(item.id), kind=str(item.definition.get("subject_kind", "prompt")), stable_key=item.subject_key, prompt_id=item.definition.get("prompt_id"), grid_cell_id=UUID(item.definition["grid_cell_id"]) if item.definition.get("grid_cell_id") else None, input_digest=str(item.definition.get("input_digest", "")), expected_output_count=item.expected_output_count, admitted_ordinals=list(range(item.expected_output_count))) for item in subjects],
        expected_artifact_count=admission.expected_artifact_count,
        expected_max_bytes=admission.permanent_reservation_bytes,
        handoff_assurance=admission.handoff_assurance,
        billing_disclosure=BILLING_DISCLOSURE if admission.handoff_assurance == "locally_verified" else None,
        required_acknowledgement=BILLING_ACKNOWLEDGEMENT if admission.handoff_assurance == "locally_verified" else None,
        block_reasons=block_reasons,
        recovery_links=[],
        controls=controls,
        canonical_route=f"#/evals/admissions/{admission.id}",
        focus_target_id=f"fal-admission-{admission.id}",
    )


@router.post("/fal-admissions", response_model=FalProjectionResultV1, status_code=status.HTTP_200_OK)
def create_fal_admission(body: FalCreateAdmissionV1, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    workspace = current_workspace(db)
    existing = db.scalar(
        select(models.FalAdmission)
        .join(models.StoragePolicySnapshot, models.StoragePolicySnapshot.id == models.FalAdmission.snapshot_id)
        .where(models.FalAdmission.workspace_id == workspace.id, models.StoragePolicySnapshot.operation_id == str(body.client_request_id))
        .order_by(models.FalAdmission.created_at.desc())
    )
    if existing is not None:
        return FalProjectionResultV1(kind="projection", admission=_projection(db, existing), announcement=_announcement(existing.id, "FAL admission already exists"))
    try:
        adapter_id = str(body.parameters.get("endpoint_id"))
        adapter = get_adapter(adapter_id)
        normalized = normalize_parameters(body.parameters, grid_cell=body.kind == "grid")
    except (KeyError, ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    model_version = get_or_404(db, models.ModelVersion, str(body.model_version_id))
    checkpoint_revision = get_or_404(db, models.CheckpointRevision, str(body.checkpoint_revision_id))
    if model_version.checkpoint_revision_id and model_version.checkpoint_revision_id != checkpoint_revision.id:
        raise HTTPException(status_code=409, detail="checkpoint revision does not match model version")
    model = get_or_404(db, models.Model, model_version.model_id)
    definition_id: str | None = None
    admission_id = str(uuid4())
    if model.project_id != str(body.project_id):
        raise HTTPException(status_code=409, detail="model version belongs to another project")
    if body.kind == "eval":
        prompts = [item.model_dump() for item in body.prompts]
        definition = db.get(models.EvalDefinition, str(body.definition_id)) if body.definition_id else db.scalar(select(models.EvalDefinition).where(models.EvalDefinition.model_version_id == model_version.id, models.EvalDefinition.endpoint == adapter.endpoint_id).order_by(models.EvalDefinition.created_at.desc()))
        if body.definition_id and (definition is None or definition.project_id != str(body.project_id) or definition.model_version_id != model_version.id or definition.endpoint != adapter.endpoint_id):
            raise HTTPException(status_code=409, detail="compiled generation definition does not match project, model version, or provider endpoint")
        definition_id = definition.id if definition else None
        input_digest = _digest({"prompts": prompts, "parameters": normalized})
        try:
            drafts = build_eval_subjects(admission_id=str(uuid4()), mode=body.mode, prompts=prompts, input_digest=input_digest, expected_output_count=int(normalized["num_images"]))
            plan_kind = "eval"
        except FalAdmissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        definition_payload = {"kind": "eval", "mode": body.mode, "prompts": prompts}
    else:
        grid = get_or_404(db, models.GridDefinition, str(body.grid_definition_id))
        if grid.project_id != str(body.project_id):
            raise HTTPException(status_code=409, detail="grid belongs to another project")
        cells = _grid_cells(grid)
        input_digest = _digest({"grid_definition_id": str(grid.id), "cells": cells, "parameters": normalized})
        try:
            drafts = build_grid_subjects(admission_id=str(uuid4()), grid_definition_id=str(grid.id), cells=cells)
            plan_kind = "grid"
        except FalAdmissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        definition_id = grid.eval_definition_id
        definition_payload = {"kind": "grid", "grid_definition_id": str(grid.id), "cells": cells}
    try:
        plan = build_admission_plan(kind=plan_kind, subjects=drafts)
        snapshot, root, _policy = _snapshot(db, workspace, x_profile_id, str(body.client_request_id))
    except FalAdmissionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    admission = models.FalAdmission(
        id=admission_id,
        workspace_id=workspace.id,
        eval_run_id=None,
        snapshot_id=snapshot.id,
        checkpoint_revision_id=checkpoint_revision.id,
        endpoint_id=adapter.endpoint_id,
        version=0,
        state="ready",
        handoff_assurance="locally_verified",
        expected_artifact_count=plan.expected_artifact_count,
        spool_root_id=root.id,
        spool_grant_bytes=plan.spool_grant_bytes,
        permanent_reservation_bytes=plan.expected_max_bytes,
        dispatch_fence=sha256(f"{body.client_request_id}:{uuid4()}".encode()).hexdigest(),
    )
    db.add(admission)
    db.flush()
    common_definition = {
        **definition_payload,
        "admission_kind": body.kind,
        "subject_kind": None,
        "project_id": str(body.project_id),
        "model_version_id": str(body.model_version_id),
        "definition_id": definition_id,
        "normalized_parameters": normalized,
        "admission_input_digest": input_digest,
    }
    for draft in drafts:
        db.add(models.FalSubject(
            admission_id=admission.id,
            ordinal=draft.ordinal,
            subject_key=draft.stable_key,
            definition={**common_definition, "subject_kind": draft.kind, "prompt_id": draft.prompt_id, "grid_cell_id": draft.grid_cell_id, "input_digest": draft.input_digest},
            expected_output_count=draft.expected_output_count,
        ))
    db.flush()
    projection = _projection(db, admission)
    db.commit()
    return FalProjectionResultV1(kind="projection", admission=projection, announcement=_announcement(admission.id, "FAL admission is ready for review"))


@router.post("/generation-requests/compile", status_code=status.HTTP_200_OK)
def compile_generation_request(body: GenerationCompileV1, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    """Persist one provider-neutral workflow request, then compile it into the existing FAL admission path."""
    workspace = current_workspace(db)
    existing = db.scalar(
        select(models.FalAdmission)
        .join(models.StoragePolicySnapshot, models.StoragePolicySnapshot.id == models.FalAdmission.snapshot_id)
        .where(models.FalAdmission.workspace_id == workspace.id, models.StoragePolicySnapshot.operation_id == str(body.client_request_id))
        .order_by(models.FalAdmission.created_at.desc())
    )
    compile_digest = _digest(body.model_dump(mode="json"))
    if existing is not None:
        subject = db.scalar(select(models.FalSubject).where(models.FalSubject.admission_id == existing.id).order_by(models.FalSubject.ordinal))
        definition_id = str(subject.definition.get("definition_id")) if subject else ""
        definition = db.get(models.EvalDefinition, definition_id) if definition_id else None
        inline = list(definition.inline_prompts or []) if definition else []
        if not inline or dict(inline[0].get("metadata") or {}).get("compile_digest") != compile_digest:
            raise HTTPException(status_code=409, detail="client_request_id was already used for a different generation request")
        return {"kind": "compiled_generation", "workflow": body.workflow, "provider": body.provider, "definition_id": definition_id or None, "prompt_set_id": None, "admission": _projection(db, existing).model_dump(mode="json")}
    if body.provider != "fal":
        raise HTTPException(status_code=422, detail=f"generation provider is not installed: {body.provider}")
    model_version = get_or_404(db, models.ModelVersion, str(body.model_version_id))
    model = get_or_404(db, models.Model, model_version.model_id)
    if model.project_id != str(body.context.project_id):
        raise HTTPException(status_code=409, detail="model version belongs to another project")
    if body.context.kind == "model" and body.context.model_id and model.id != str(body.context.model_id):
        raise HTTPException(status_code=409, detail="model context does not match selected model version")
    if model_version.checkpoint_revision_id != str(body.checkpoint_revision_id):
        raise HTTPException(status_code=409, detail="checkpoint revision does not match model version")
    readiness = dict(model_version.readiness or {})
    fal_url = str(readiness.get("fal_url") or readiness.get("fal_path") or "")
    endpoint = str(readiness.get("endpoint_id") or readiness.get("endpoint") or readiness.get("fal_endpoint") or "")
    requested_endpoint = str(body.parameters.get("endpoint_id") or endpoint)
    if not fal_url:
        raise HTTPException(status_code=409, detail="selected model version is not registered with FAL")
    if not endpoint:
        raise HTTPException(status_code=409, detail="selected model version has no registered FAL endpoint")
    try:
        adapter = get_adapter(requested_endpoint)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"requested FAL endpoint is unavailable: {requested_endpoint}") from exc
    if requested_endpoint != endpoint and not adapter.supports_base_model(model_version.base_model):
        raise HTTPException(status_code=409, detail=f"requested endpoint is incompatible with base model {model_version.base_model}")
    endpoint = requested_endpoint
    prompt = apply_generation_prompt_settings(db, body.prompt)
    inline_prompt_id = str(uuid5(NAMESPACE_URL, f"titles-dam:generation:{body.client_request_id}:0"))
    inline_prompts = [{"id": inline_prompt_id, "text": prompt, "position": 0, "metadata": {"workflow": body.workflow, "client_request_id": str(body.client_request_id), "compile_digest": compile_digest}}]
    definition = models.EvalDefinition(project_id=model.project_id, name=f"Image request {body.client_request_id}", endpoint=endpoint, model_version_id=model_version.id, prompt_set_id=None, inline_prompts=inline_prompts, parameters=dict(body.parameters))
    db.add(definition)
    db.flush()
    projection = create_fal_admission(FalEvalCreateV1(kind="eval", mode="single", project_id=body.context.project_id, model_version_id=body.model_version_id, checkpoint_revision_id=body.checkpoint_revision_id, prompts=[FalPromptV1(prompt_id=inline_prompt_id, prompt=prompt)], parameters=body.parameters, client_request_id=body.client_request_id, definition_id=UUID(definition.id)), db, x_profile_id)
    return {"kind": "compiled_generation", "workflow": body.workflow, "provider": body.provider, "definition_id": definition.id, "prompt_set_id": None, "admission": projection.admission.model_dump(mode="json")}


@router.post("/eval-generation-requests/compile", status_code=status.HTTP_200_OK)
def compile_eval_generation_request(body: EvalGenerationCompileV1, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    """Compile one model slice of an ordered Eval workflow with an immutable prompt source."""
    if body.provider != "fal":
        raise HTTPException(status_code=422, detail=f"generation provider is not installed: {body.provider}")
    workspace = current_workspace(db)
    existing = db.scalar(select(models.FalAdmission).join(models.StoragePolicySnapshot,
        models.StoragePolicySnapshot.id == models.FalAdmission.snapshot_id).where(
        models.FalAdmission.workspace_id == workspace.id,
        models.StoragePolicySnapshot.operation_id == str(body.client_request_id)).order_by(models.FalAdmission.created_at.desc()))
    digest = _digest(body.model_dump(mode="json"))
    if existing:
        subject = db.scalar(select(models.FalSubject).where(models.FalSubject.admission_id == existing.id).order_by(models.FalSubject.ordinal))
        definition = db.get(models.EvalDefinition, str((subject.definition or {}).get("definition_id"))) if subject else None
        if not definition or dict((definition.inline_prompts or [{}])[0].get("metadata") or {}).get("compile_digest") != digest:
            raise HTTPException(status_code=409, detail="client_request_id was already used for a different generation request")
        return {"kind": "compiled_generation", "workflow": "eval", "provider": body.provider,
            "definition_id": definition.id, "prompt_set_id": definition.prompt_set_id, "admission": _projection(db, existing).model_dump(mode="json")}
    version = get_or_404(db, models.ModelVersion, str(body.model_version_id))
    model = get_or_404(db, models.Model, version.model_id)
    if model.project_id != str(body.context.project_id):
        raise HTTPException(status_code=409, detail="model version belongs to another project")
    if body.context.kind == "model" and body.context.model_id and model.id != str(body.context.model_id):
        raise HTTPException(status_code=409, detail="model context does not match selected model version")
    if version.checkpoint_revision_id != str(body.checkpoint_revision_id):
        raise HTTPException(status_code=409, detail="checkpoint revision does not match model version")
    readiness = dict(version.readiness or {})
    fal_url = str(readiness.get("fal_url") or readiness.get("fal_path") or "")
    endpoint = str(readiness.get("endpoint_id") or readiness.get("endpoint") or readiness.get("fal_endpoint") or "")
    if not fal_url:
        raise HTTPException(status_code=409, detail="selected model version is not registered with FAL")
    if not endpoint:
        raise HTTPException(status_code=409, detail="selected model version has no registered FAL endpoint")
    try:
        get_adapter(endpoint)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"registered FAL endpoint is unavailable: {endpoint}") from exc
    prompt_set = None
    if body.prompt_set_id:
        prompt_set = get_or_404(db, models.PromptSet, str(body.prompt_set_id))
        if prompt_set.project_id != model.project_id:
            raise HTTPException(status_code=409, detail="prompt set version belongs to another project")
        saved_prompts = list(db.scalars(select(models.Prompt).where(
            models.Prompt.prompt_set_id == prompt_set.id
        ).order_by(models.Prompt.position, models.Prompt.id)))
        submitted = [(str(prompt.prompt_id), prompt.prompt) for prompt in body.prompts]
        expected = [(prompt.id, prompt.text) for prompt in saved_prompts]
        if submitted != expected:
            raise HTTPException(status_code=409, detail="submitted prompts do not match the selected prompt set version")
    compiled_prompts = [
        FalPromptV1(prompt_id=str(prompt.prompt_id), prompt=apply_generation_prompt_settings(db, prompt.prompt))
        for prompt in body.prompts
    ]
    inline_prompts = [{"id": prompt.prompt_id, "text": prompt.prompt, "position": index,
        "metadata": {"workflow": "eval", "client_request_id": str(body.client_request_id), "compile_digest": digest}}
        for index, prompt in enumerate(compiled_prompts)]
    source_name = f" · {prompt_set.name} v{prompt_set.version}" if prompt_set else ""
    definition = models.EvalDefinition(project_id=model.project_id, name=f"Eval · {model.name}{source_name} · {len(inline_prompts)} prompts",
        endpoint=endpoint, model_version_id=version.id, prompt_set_id=prompt_set.id if prompt_set else None, inline_prompts=inline_prompts,
        parameters={**body.parameters, "endpoint_id": endpoint})
    db.add(definition); db.flush()
    projection = create_fal_admission(FalEvalCreateV1(kind="eval", mode="single" if len(compiled_prompts) == 1 else "prompt_batch",
        project_id=body.context.project_id, model_version_id=body.model_version_id,
        checkpoint_revision_id=body.checkpoint_revision_id, prompts=compiled_prompts,
        parameters={**body.parameters, "endpoint_id": endpoint}, client_request_id=body.client_request_id,
        definition_id=UUID(definition.id)), db, x_profile_id)
    return {"kind": "compiled_generation", "workflow": "eval", "provider": body.provider,
        "definition_id": definition.id, "prompt_set_id": definition.prompt_set_id, "admission": projection.admission.model_dump(mode="json")}

@router.get("/fal-admissions/{admission_id}", response_model=FalAdmissionV1)
def get_fal_admission(admission_id: str, db: DB):
    admission = get_or_404(db, models.FalAdmission, admission_id)
    return _projection(db, admission)


@router.post("/fal-admissions/{admission_id}/admit", response_model=FalAdmissionResultV1, status_code=status.HTTP_202_ACCEPTED)
def admit_fal_admission(admission_id: str, body: FalAdmitRequestV1, db: DB, x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None):
    admission = get_or_404(db, models.FalAdmission, admission_id)
    current = _projection(db, admission)
    if admission.version != body.expected_version or admission.state != "ready":
        result = FalConflictResultV1(
            kind="conflict",
            current=current,
            canonical_route=current.canonical_route,
            focus_target_id=current.focus_target_id,
            announcement=_announcement(admission.id, "FAL admission changed; review the current admission", mode="assertive"),
        )
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=result.model_dump(mode="json"))
    if body.billing_acknowledgement != BILLING_ACKNOWLEDGEMENT:
        result = FalConflictResultV1(
            kind="conflict",
            current=current,
            canonical_route=current.canonical_route,
            focus_target_id=current.focus_target_id,
            announcement=_announcement(admission.id, "Billing acknowledgement is required before admission", mode="assertive"),
        )
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=result.model_dump(mode="json"))
    subjects = list(db.scalars(select(models.FalSubject).where(models.FalSubject.admission_id == admission.id).order_by(models.FalSubject.ordinal)))
    if not subjects:
        raise HTTPException(status_code=409, detail="FAL admission has no subjects")
    definition_id = subjects[0].definition.get("definition_id")
    definition = db.get(models.EvalDefinition, definition_id) if definition_id else None
    if definition is None:
        raise HTTPException(status_code=409, detail="eval definition is required before admission")
    checkpoint_revision = get_or_404(db, models.CheckpointRevision, admission.checkpoint_revision_id)
    run = models.EvalRun(
        definition_id=definition.id,
        checkpoint_id=checkpoint_revision.checkpoint_id,
        checkpoint_revision_id=admission.checkpoint_revision_id,
        status="queued",
        parameters_snapshot=current.frozen.get("normalized_parameters", {}),
    )
    db.add(run)
    db.flush()
    for subject in subjects:
        db.add(models.FalSubmissionIntent(
            admission_id=admission.id,
            subject_id=subject.id,
            generation=subject.generation,
            request_digest=sha256(f"{admission.id}:{subject.id}:{subject.generation}".encode()).hexdigest(),
            state="planned",
            submission_fence=admission.dispatch_fence,
        ))
    admission.eval_run_id = run.id
    admission.state = "admitted"
    admission.version += 1
    admission.billing_acknowledged_at = datetime.now(timezone.utc)
    actor = active_profile(db, x_profile_id)
    job = models.Job(
        workspace_id=admission.workspace_id,
        kind="fal.eval_admission",
        profile_id=actor.id,
        payload={"admission_id": admission.id, "eval_run_id": run.id, "storage_contract_version": 1},
    )
    db.add(job)
    record_activity(db, action="fal.admission_admitted", subject_type="fal_admission", subject_id=admission.id, profile_id=actor.id, project_id=definition.project_id, details={"eval_run_id": run.id, "job_id": job.id})
    db.commit()
    projection = _projection(db, admission)
    return FalAcceptedResultV1(kind="accepted", admission=projection, eval_run_id=UUID(run.id), canonical_route=f"#/evals/{run.id}", focus_target_id=f"eval-run-{run.id}", announcement=_announcement(admission.id, "FAL run accepted"))


__all__ = [
    "FalAdmissionResultV1",
    "FalAdmitRequestV1",
    "FalCreateAdmissionV1",
    "FalAdmissionV1",
    "router",
]
