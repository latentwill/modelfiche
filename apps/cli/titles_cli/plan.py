from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


CONTRACT_VERSION = "2026-07-22.v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _nonempty(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} is required")
    return value


def load_prompts(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read prompt file {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("prompt file must contain a JSON array")
    return normalize_cases(raw)


def normalize_cases(values: list[Any], *, prompt_flag: str = "prompt") -> list[dict[str, Any]]:
    if not values:
        raise ValueError("at least one inline prompt or prompt file entry is required")
    cases: list[dict[str, Any]] = []
    for ordinal, value in enumerate(values):
        if isinstance(value, str):
            prompt = value
            variables: dict[str, Any] = {}
        elif isinstance(value, Mapping):
            input_value = value.get("input", value)
            if not isinstance(input_value, Mapping):
                raise ValueError(f"case {ordinal} input must be an object")
            prompt = input_value.get(prompt_flag, input_value.get("text"))
            variables = input_value.get("variables", {})
            if not isinstance(variables, Mapping):
                raise ValueError(f"case {ordinal} variables must be an object")
        else:
            raise ValueError(f"case {ordinal} must be a string or object")
        prompt = _nonempty(str(prompt or ""), f"case {ordinal} prompt")
        case_input = {"prompt": prompt, "variables": dict(variables)}
        case_id = f"case_{ordinal}"
        cases.append({"case_id": case_id, "ordinal": ordinal, "input": case_input, "input_digest": digest(case_input)})
    return cases


def parse_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def parse_axis(name: str, values: list[str]) -> dict[str, Any]:
    name = _nonempty(name, "axis name")
    if not values:
        raise ValueError(f"axis {name} requires at least one value")
    parsed: list[dict[str, Any]] = []
    for raw in values:
        value: Any
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        parsed.append({"value": value, "digest": digest(value)})
    return {"name": name, "values": parsed}


@dataclass(frozen=True, slots=True)
class PlanTarget:
    target_id: str
    ordinal: int
    provider: str
    endpoint_id: str
    model_version_id: str | None = None
    checkpoint_revision_id: str | None = None
    schema_digest: str = ""
    schema: dict[str, Any] = field(default_factory=dict)
    fixed_target: dict[str, Any] = field(default_factory=dict)
    shared_overrides: dict[str, Any] = field(default_factory=dict)
    target_overrides: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id, "ordinal": self.ordinal, "provider": self.provider,
            "endpoint_id": self.endpoint_id, "model_version_id": self.model_version_id,
            "checkpoint_revision_id": self.checkpoint_revision_id, "schema_digest": self.schema_digest,
            "schema": self.schema, "fixed_target": self.fixed_target or {
                "provider": self.provider, "endpoint_id": self.endpoint_id,
                "model_version_id": self.model_version_id, "checkpoint_revision_id": self.checkpoint_revision_id,
            }, "shared_overrides": self.shared_overrides, "target_overrides": self.target_overrides,
        }


def make_plan(*, project_id: str, plan_id: str, targets: list[PlanTarget], cases: list[dict[str, Any]], x: dict[str, Any], y: dict[str, Any], z: dict[str, Any] | None = None, shared_params: dict[str, Any] | None = None, endpoint_defaults: dict[str, Any] | None = None, plan_version: int = 1) -> dict[str, Any]:
    project_id = _nonempty(project_id, "project")
    plan_id = _nonempty(plan_id, "plan")
    if len({target.target_id for target in targets}) != len(targets):
        raise ValueError("target IDs must be unique")
    if not targets:
        raise ValueError("at least one target is required")
    axes = {"x": x, "y": y, "z": z}
    if x.get("name") == y.get("name"):
        raise ValueError("X and Y axes must use different names")
    snapshot = {"contract_version": CONTRACT_VERSION, "project_id": project_id, "plan_id": plan_id, "plan_version": plan_version, "axes": axes, "cases": cases, "targets": [target.as_dict() for target in targets], "shared_params": shared_params or {}, "endpoint_defaults": endpoint_defaults or {}}
    snapshot["digest"] = digest(snapshot)
    return snapshot


def request_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize only client-owned plan inputs for create/preflight requests."""
    axes = {}
    for axis_name in ("x", "y", "z"):
        axis = plan.get("axes", {}).get(axis_name)
        if axis is None:
            axes[axis_name] = None
        else:
            axes[axis_name] = {
                "name": axis["name"],
                "values": [item["value"] if isinstance(item, Mapping) and "value" in item else item for item in axis.get("values", [])],
            }
    cases = [{"case_id": item["case_id"], "ordinal": item["ordinal"], "input": item["input"]} for item in plan.get("cases", [])]
    targets = []
    for item in plan.get("targets", []):
        overrides = item.get("overrides", item.get("target_overrides", {}))
        targets.append({
            key: item[key]
            for key in ("target_id", "ordinal", "provider", "endpoint_id", "model_version_id", "checkpoint_revision_id")
            if key in item and item[key] is not None
        } | {"overrides": dict(overrides or {})})
    return {
        "contract_version": plan.get("contract_version", CONTRACT_VERSION),
        "axes": axes,
        "cases": cases,
        "targets": targets,
        "shared_params": dict(plan.get("shared_params") or {}),
    }


def validate_cardinality(plan: Mapping[str, Any]) -> None:
    axes = plan.get("axes") or {}
    names = {str((axes.get(key) or {}).get("name", "")).strip().lower() for key in ("x", "y", "z") if axes.get(key)}
    target_count = len(plan.get("targets") or [])
    case_count = len(plan.get("cases") or [])
    if target_count > 1 and "target" not in names:
        raise ValueError("multiple targets require a target axis")
    if case_count > 1 and not ({"case", "prompt"} & names):
        raise ValueError("multiple cases require a case or prompt axis")
