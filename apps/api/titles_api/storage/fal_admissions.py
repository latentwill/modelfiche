from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal, Mapping, Sequence


MAX_SUBJECTS = 100
MAX_TOTAL_ARTIFACTS = 100
MAX_TOTAL_BYTES = 10 * 1024 * 1024 * 1024
MAX_ARTIFACTS_PER_SUBJECT = 16
SPOOL_BYTES_PER_SLOT = 100 * 1024 * 1024
MAX_SPOOL_SLOTS = 2


@dataclass(frozen=True, slots=True)
class FalAdmissionPlan:
    """Immutable pre-admission projection shared by Eval and grid callers."""

    kind: Literal["eval", "grid"]
    input_digest: str
    subjects: tuple[FalSubjectDraft, ...]
    expected_artifact_count: int
    expected_max_bytes: int
    spool_grant_bytes: int


def build_admission_plan(
    *,
    kind: Literal["eval", "grid"],
    subjects: Sequence[FalSubjectDraft],
) -> FalAdmissionPlan:
    """Validate aggregate limits before any persistence, reservation, or billing."""
    if kind not in {"eval", "grid"}:
        raise FalAdmissionValidationError("admission kind must be eval or grid")
    frozen = tuple(subjects)
    if not frozen:
        raise FalAdmissionValidationError("admission requires at least one subject")
    if len(frozen) > MAX_SUBJECTS:
        raise FalAdmissionValidationError("admission exceeds the maximum subject count")
    ordinals = [subject.ordinal for subject in frozen]
    if len({subject.stable_key for subject in frozen}) != len(frozen):
        raise FalAdmissionValidationError("admission subject keys must be unique")
    for subject in frozen:
        count = subject.expected_output_count
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_ARTIFACTS_PER_SUBJECT:
            raise FalAdmissionValidationError("subject expected output count is invalid")
        if len(subject.admitted_ordinals) != count:
            raise FalAdmissionValidationError("subject expected output ordinals are malformed")
        if tuple(subject.admitted_ordinals) != tuple(range(count)):
            raise FalAdmissionValidationError("subject admitted ordinals must be contiguous")
    if ordinals != list(range(len(frozen))):
        raise FalAdmissionValidationError("admission subject ordinals must be contiguous")
    expected_artifacts = sum(subject.expected_output_count for subject in frozen)
    if expected_artifacts > MAX_TOTAL_ARTIFACTS:
        raise FalAdmissionValidationError("admission exceeds the maximum artifact count")
    if any(subject.expected_output_count > MAX_ARTIFACTS_PER_SUBJECT for subject in frozen):
        raise FalAdmissionValidationError("subject exceeds the maximum artifact count")
    expected_max_bytes = expected_artifacts * 100 * 1024 * 1024
    if expected_max_bytes > MAX_TOTAL_BYTES:
        raise FalAdmissionValidationError("admission exceeds the maximum artifact bytes")
    slots = min(MAX_SPOOL_SLOTS, max(1, expected_artifacts))
    payload = [
        {
            "ordinal": subject.ordinal,
            "kind": subject.kind,
            "stable_key": subject.stable_key,
            "prompt_id": subject.prompt_id,
            "grid_cell_id": subject.grid_cell_id,
            "input_digest": subject.input_digest,
            "expected_output_count": subject.expected_output_count,
            "admitted_ordinals": list(subject.admitted_ordinals),
        }
        for subject in frozen
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return FalAdmissionPlan(
        kind=kind,
        input_digest=hashlib.sha256(encoded).hexdigest(),
        subjects=frozen,
        expected_artifact_count=expected_artifacts,
        expected_max_bytes=expected_max_bytes,
        spool_grant_bytes=slots * SPOOL_BYTES_PER_SLOT,
    )


BILLING_DISCLOSURE = "FAL cannot preflight checkpoint access; this run may be billed if checkpoint fetch fails"
BILLING_ACKNOWLEDGEMENT = "I understand FAL may bill this run even if checkpoint fetch fails"


class FalAdmissionValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FalSubjectDraft:
    ordinal: int
    kind: Literal["single", "prompt", "grid_cell"]
    stable_key: str
    prompt_id: str | None
    grid_cell_id: str | None
    input_digest: str
    expected_output_count: int
    admitted_ordinals: tuple[int, ...]


def build_eval_subjects(
    *,
    admission_id: str,
    mode: Literal["single", "prompt_batch"],
    prompts: Sequence[Mapping[str, str]],
    input_digest: str,
    expected_output_count: int,
) -> list[FalSubjectDraft]:
    _validate_identifier("admission_id", admission_id)
    input_digest = input_digest.lower()
    _validate_digest(input_digest)
    _validate_eval_output_count(expected_output_count)
    if mode == "single" and len(prompts) != 1:
        raise FalAdmissionValidationError("single eval requires exactly one prompt")
    if mode == "prompt_batch" and not prompts:
        raise FalAdmissionValidationError("prompt batch requires at least one prompt")

    subjects: list[FalSubjectDraft] = []
    prompt_ids: set[str] = set()
    for ordinal, prompt in enumerate(prompts):
        prompt_id = prompt.get("prompt_id")
        prompt_text = prompt.get("prompt")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise FalAdmissionValidationError("each prompt requires a prompt_id")
        if not isinstance(prompt_text, str) or not prompt_text.strip():
            raise FalAdmissionValidationError("each prompt requires prompt text")
        if prompt_id in prompt_ids:
            raise FalAdmissionValidationError("prompt batch prompt_ids must be unique")
        prompt_ids.add(prompt_id)
        kind: Literal["single", "prompt"] = "single" if mode == "single" else "prompt"
        stable_key = (
            f"{admission_id}:single:{admission_id}"
            if kind == "single"
            else f"{admission_id}:prompt:{prompt_id}:{input_digest}"
        )
        subjects.append(
            FalSubjectDraft(
                ordinal=ordinal,
                kind=kind,
                stable_key=stable_key,
                prompt_id=prompt_id,
                grid_cell_id=None,
                input_digest=input_digest,
                expected_output_count=expected_output_count,
                admitted_ordinals=tuple(range(expected_output_count)),
            )
        )
    return subjects



def build_grid_subjects(
    *,
    admission_id: str,
    grid_definition_id: str,
    cells: Sequence[Mapping[str, Any]],
) -> list[FalSubjectDraft]:
    _validate_identifier("admission_id", admission_id)
    _validate_identifier("grid_definition_id", grid_definition_id)
    if not cells:
        raise FalAdmissionValidationError("grid requires at least one frozen cell")

    prepared: list[tuple[str, int, int, str]] = []
    cell_ids: set[str] = set()
    coordinates: set[tuple[int, int]] = set()
    for cell in cells:
        cell_id = cell.get("grid_cell_id")
        x_index = cell.get("x_index")
        y_index = cell.get("y_index")
        axis_digest = cell.get("axis_digest")
        if not isinstance(cell_id, str) or not cell_id:
            raise FalAdmissionValidationError("each grid cell requires a grid_cell_id")
        if not isinstance(x_index, int) or isinstance(x_index, bool) or x_index < 0:
            raise FalAdmissionValidationError("grid x_index must be a non-negative integer")
        if not isinstance(y_index, int) or isinstance(y_index, bool) or y_index < 0:
            raise FalAdmissionValidationError("grid y_index must be a non-negative integer")
        if cell_id in cell_ids or (x_index, y_index) in coordinates:
            raise FalAdmissionValidationError("grid cells must have unique IDs and coordinates")
        _validate_digest(axis_digest)
        cell_ids.add(cell_id)
        coordinates.add((x_index, y_index))
        prepared.append((cell_id, x_index, y_index, axis_digest))

    return [
        FalSubjectDraft(
            ordinal=ordinal,
            kind="grid_cell",
            stable_key=f"{admission_id}:grid_cell:{grid_definition_id}:{x_index}:{y_index}:{axis_digest}",
            prompt_id=None,
            grid_cell_id=cell_id,
            input_digest=axis_digest,
            expected_output_count=1,
            admitted_ordinals=(0,),
        )
        for ordinal, (cell_id, x_index, y_index, axis_digest) in enumerate(
            sorted(prepared, key=lambda item: (item[2], item[1], item[0]))
        )
    ]



@dataclass(frozen=True, slots=True)
class FalRecoveryPlan:
    kind: Literal["resume_same_request", "retry_failed_subjects"]
    subject_generations: dict[str, int]
    billable: bool


def plan_recovery_action(
    *,
    action: Literal["resume_same_request", "retry_failed_subjects"],
    subjects: Sequence[Mapping[str, Any]],
    selected_subject_ids: Sequence[str],
    retry_requests: Sequence[Mapping[str, Any]],
    billing_acknowledgement: str | None,
) -> FalRecoveryPlan:
    by_id: dict[str, tuple[int, str]] = {}
    for subject in subjects:
        subject_id = subject.get("id")
        generation = subject.get("generation")
        state = subject.get("state")
        if not isinstance(subject_id, str) or not subject_id:
            raise FalAdmissionValidationError("recovery subject ID is required")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise FalAdmissionValidationError("recovery subject generation is invalid")
        if not isinstance(state, str):
            raise FalAdmissionValidationError("recovery subject state is required")
        if subject_id in by_id:
            raise FalAdmissionValidationError("recovery subject IDs must be unique")
        by_id[subject_id] = (generation, state)

    if action == "resume_same_request":
        if retry_requests or billing_acknowledgement is not None:
            raise FalAdmissionValidationError("same-request resume cannot include billing retry input")
        selected = tuple(selected_subject_ids)
        if not selected or len(selected) != len(set(selected)):
            raise FalAdmissionValidationError("same-request resume requires unique selected subjects")
        generations: dict[str, int] = {}
        for subject_id in selected:
            value = by_id.get(subject_id)
            if value is None or value[1] != "retryable":
                raise FalAdmissionValidationError("same-request resume requires retryable subjects")
            generations[subject_id] = value[0]
        return FalRecoveryPlan(kind=action, subject_generations=generations, billable=False)

    if action == "retry_failed_subjects":
        if selected_subject_ids or billing_acknowledgement != BILLING_ACKNOWLEDGEMENT:
            raise FalAdmissionValidationError("billable retry requires the exact billing acknowledgement")
        if not retry_requests:
            raise FalAdmissionValidationError("billable retry requires selected failed subjects")
        generations = {}
        for request in retry_requests:
            subject_id = request.get("subject_id")
            expected_generation = request.get("expected_terminal_generation")
            value = by_id.get(subject_id) if isinstance(subject_id, str) else None
            if value is None or value[1] != "failed":
                raise FalAdmissionValidationError("billable retry requires failed subjects")
            if expected_generation != value[0]:
                raise FalAdmissionValidationError("stale terminal generation")
            if subject_id in generations:
                raise FalAdmissionValidationError("billable retry subjects must be unique")
            generations[subject_id] = value[0] + 1
        return FalRecoveryPlan(kind=action, subject_generations=generations, billable=True)

    raise FalAdmissionValidationError("unknown recovery action")
def validate_billing_acknowledgement(
    handoff_assurance: Literal["locally_verified", "provider_verified"],
    acknowledgement: str | None,
) -> None:
    if handoff_assurance == "locally_verified":
        if acknowledgement != BILLING_ACKNOWLEDGEMENT:
            raise FalAdmissionValidationError("locally verified handoff requires the exact billing acknowledgement")
        return
    if handoff_assurance == "provider_verified":
        if acknowledgement is not None:
            raise FalAdmissionValidationError("provider verified handoff acknowledgement must be null")
        return
    raise FalAdmissionValidationError("unknown handoff assurance")


def _validate_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise FalAdmissionValidationError(f"{name} is required")


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise FalAdmissionValidationError("input_digest must be a SHA-256 hex digest")


def _validate_eval_output_count(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 4:
        raise FalAdmissionValidationError("expected_output_count must be an integer from 1 to 4")
