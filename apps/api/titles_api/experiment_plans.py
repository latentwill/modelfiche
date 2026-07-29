from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from itertools import product
from typing import Any, Mapping

from .integrations.fal.adapters import ValidationError, get_adapter

CONTRACT_VERSION = "2026-07-22.v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _value(raw: Any) -> Any:
    if isinstance(raw, Mapping) and "value" in raw:
        return raw["value"]
    return raw


def normalize_case(raw: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    case_id = str(raw.get("case_id") or raw.get("id") or "")
    if not case_id:
        raise ValueError("each case requires case_id")
    input_value = raw.get("input")
    if input_value is None:
        input_value = {"prompt": raw.get("prompt") or raw.get("text", ""), "variables": dict(raw.get("variables") or {})}
    if not isinstance(input_value, Mapping):
        raise ValueError(f"case {case_id} input must be an object")
    result = {"case_id": case_id, "ordinal": int(raw.get("ordinal", ordinal)), "input": deepcopy(dict(input_value))}
    result["input_digest"] = digest(result["input"])
    return result


def _adapter_schema(endpoint_id: str) -> tuple[dict[str, Any], str]:
    adapter = get_adapter(endpoint_id)
    schema = {
        "request_fields": sorted(adapter.allowed_request_fields),
        "defaults": deepcopy(adapter.defaults),
        "fields": deepcopy(adapter.field_schema),
        "grid": adapter.grid_contract(),
    }
    return schema, digest(schema)


def normalize_target(raw: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    spoofed = {"schema", "schema_digest", "fixed_target", "request_fields", "defaults", "fields", "grid"}
    if "registered_lora" in raw and not raw.get("_server_hydrated"):
        raise ValueError("target LoRA registration is server-owned")
    if spoofed.intersection(raw):
        raise ValueError("target schema/default snapshots are server-owned")
    provider = str(raw.get("provider") or "fal")
    if provider != "fal":
        raise ValueError("target provider must be fal")
    endpoint_id = str(raw.get("endpoint_id") or raw.get("endpoint") or "")
    if not endpoint_id:
        raise ValueError("each target requires endpoint_id")
    schema, schema_digest = _adapter_schema(endpoint_id)
    target_id = str(raw.get("target_id") or raw.get("id") or "")
    if not target_id:
        raise ValueError("each target requires target_id")
    overrides = raw.get("overrides", raw.get("target_overrides", {}))
    if not isinstance(overrides, Mapping):
        raise ValueError(f"target {target_id} overrides must be an object")
    result = {
        "target_id": target_id,
        "ordinal": int(raw.get("ordinal", ordinal)),
        "provider": "fal",
        "endpoint_id": endpoint_id,
        "model_version_id": raw.get("model_version_id"),
        "checkpoint_revision_id": raw.get("checkpoint_revision_id"),
        "schema_digest": schema_digest,
        "schema": schema,
        "fixed_target": {
            "provider": "fal",
            "endpoint_id": endpoint_id,
            "model_version_id": raw.get("model_version_id"),
            "checkpoint_revision_id": raw.get("checkpoint_revision_id"),
        },
        "shared_overrides": deepcopy(dict(raw.get("shared_overrides") or {})),
        "target_overrides": deepcopy(dict(overrides)),
    }
    if raw.get("registered_lora"):
        result["registered_lora"] = deepcopy(dict(raw["registered_lora"]))
    return result


def normalize_plan(raw: Mapping[str, Any], project_id: str) -> dict[str, Any]:
    if str(raw.get("contract_version") or CONTRACT_VERSION) != CONTRACT_VERSION:
        raise ValueError(f"unsupported contract_version; expected {CONTRACT_VERSION}")
    axes = raw.get("axes")
    if not isinstance(axes, Mapping):
        raise ValueError("plan axes are required")
    normalized_axes: dict[str, Any] = {}
    names: set[str] = set()
    for key in ("x", "y", "z"):
        axis = axes.get(key)
        if axis is None:
            if key != "z":
                raise ValueError(f"{key} axis is required")
            normalized_axes[key] = None
            continue
        if not isinstance(axis, Mapping):
            raise ValueError(f"{key} axis must be an object")
        name = str(axis.get("name") or axis.get("parameter") or axis.get("field") or "")
        values = list(axis.get("values") or [])
        if not name or not values:
            raise ValueError(f"{key} axis requires name and values")
        clean_values = [_value(item) for item in values]
        if len({canonical_json(item) for item in clean_values}) != len(clean_values):
            raise ValueError(f"{key} axis values must be unique")
        normalized_axes[key] = {"name": name, "values": clean_values}
        names.add(name)
    if len({normalized_axes[axis]["name"] for axis in ("x", "y")}) != 2:
        raise ValueError("x and y axes must use different names")
    if normalized_axes["z"] and normalized_axes["z"]["name"] in {normalized_axes["x"]["name"], normalized_axes["y"]["name"]}:
        raise ValueError("x, y, and z axes must use different names")
    raw_cases = list(raw.get("cases") or [])
    raw_targets = list(raw.get("targets") or [])
    if not raw_cases or not raw_targets:
        raise ValueError("plan requires at least one inline case and target")
    cases = [normalize_case(item, index) for index, item in enumerate(raw_cases)]
    targets = [normalize_target(item, index) for index, item in enumerate(raw_targets)]
    if len({item["case_id"] for item in cases}) != len(cases) or len({item["ordinal"] for item in cases}) != len(cases):
        raise ValueError("case ids and ordinals must be unique")
    if len({item["target_id"] for item in targets}) != len(targets) or len({item["ordinal"] for item in targets}) != len(targets):
        raise ValueError("target ids and ordinals must be unique")
    fixed_case_id = raw.get("fixed_case_id")
    fixed_target_id = raw.get("fixed_target_id")
    case_axis = next((axis for axis in normalized_axes.values() if axis and axis["name"] in {"prompt", "case"}), None)
    target_axis = next((axis for axis in normalized_axes.values() if axis and axis["name"] == "target"), None)
    if case_axis is None:
        if fixed_case_id is None:
            if len(cases) == 1:
                fixed_case_id = cases[0]["case_id"]
            else:
                raise ValueError("fixed_case_id is required when prompt/case is not an axis")
        if str(fixed_case_id) not in {item["case_id"] for item in cases}:
            raise ValueError("fixed_case_id must reference a case")
    if target_axis is None:
        if fixed_target_id is None:
            if len(targets) == 1:
                fixed_target_id = targets[0]["target_id"]
            else:
                raise ValueError("fixed_target_id is required when target is not an axis")
        if str(fixed_target_id) not in {item["target_id"] for item in targets}:
            raise ValueError("fixed_target_id must reference a target")
    return {
        "contract_version": CONTRACT_VERSION,
        "project_id": str(project_id),
        "plan_version": int(raw.get("plan_version") or 1),
        "axes": normalized_axes,
        "cases": sorted(cases, key=lambda item: (item["ordinal"], item["case_id"])),
        "targets": sorted(targets, key=lambda item: (item["ordinal"], item["target_id"])),
        "fixed_case_id": str(fixed_case_id) if fixed_case_id is not None else None,
        "fixed_target_id": str(fixed_target_id) if fixed_target_id is not None else None,
        "shared_params": deepcopy(dict(raw.get("shared_params") or {})),
        "endpoint_defaults": {target["endpoint_id"]: deepcopy(target["schema"]["defaults"]) for target in targets},
    }


def _selected(items: list[dict[str, Any]], axis: dict[str, Any] | None, fixed_id: str | None, field: str) -> list[dict[str, Any]]:
    if axis is None:
        return [item for item in items if item["%s_id" % field] == fixed_id]
    allowed = {str(_value(value)) for value in axis["values"]}
    return [item for item in items if item["%s_id" % field] in allowed]


def _axis_value(axes: dict[str, Any], name: str, coordinate: dict[str, int | None]) -> Any:
    for key, index in coordinate.items():
        axis = axes.get(key)
        if axis and axis["name"] == name and index is not None:
            return axis["values"][index]
    return None


def resolve_plan(raw: Mapping[str, Any], project_id: str) -> dict[str, Any]:
    plan = normalize_plan(raw, project_id)
    axes = plan["axes"]
    target_axis = next((axis for axis in axes.values() if axis and axis["name"] == "target"), None)
    case_axis = next((axis for axis in axes.values() if axis and axis["name"] in {"prompt", "case"}), None)
    target_ids = {item["target_id"] for item in plan["targets"]}
    case_ids = {item["case_id"] for item in plan["cases"]}
    if target_axis:
        requested_targets = {str(_value(item)) for item in target_axis["values"]}
        if not requested_targets.issubset(target_ids):
            missing = sorted(requested_targets - target_ids)
            raise ValueError(f"target axis references unknown target IDs: {', '.join(missing)}")
        if target_ids != requested_targets:
            raise ValueError("targets not selected by target axis must be removed or made explicit in another plan")
    if case_axis:
        requested_cases = {str(_value(item)) for item in case_axis["values"]}
        if not requested_cases.issubset(case_ids):
            missing = sorted(requested_cases - case_ids)
            raise ValueError(f"case axis references unknown case IDs: {', '.join(missing)}")
        if case_ids != requested_cases:
            raise ValueError("cases not selected by prompt/case axis must be removed or made explicit in another plan")
    selected_targets = _selected(plan["targets"], target_axis, plan["fixed_target_id"], "target")
    selected_cases = _selected(plan["cases"], case_axis, plan["fixed_case_id"], "case")
    if not selected_targets or not selected_cases:
        raise ValueError("axis selections must reference existing target and case IDs")
    axis_keys = [key for key in ("x", "y", "z") if axes.get(key)]
    axis_ranges = [range(len(axes[key]["values"])) for key in axis_keys]
    cells: list[dict[str, Any]] = []
    ordinal = 0
    for coordinate_values in product(*axis_ranges):
        coordinate = {"x": None, "y": None, "z": None}
        for key, index in zip(axis_keys, coordinate_values):
            coordinate[key] = index
        case_candidates = selected_cases
        target_candidates = selected_targets
        if case_axis:
            selected_case_id = str(_axis_value(axes, case_axis["name"], coordinate))
            case_candidates = [item for item in selected_cases if item["case_id"] == selected_case_id]
        if target_axis:
            selected_target_id = str(_axis_value(axes, target_axis["name"], coordinate))
            target_candidates = [item for item in selected_targets if item["target_id"] == selected_target_id]
        if not case_candidates or not target_candidates:
            raise ValueError("axis selections must reference existing target and case IDs")
        for case in case_candidates:
            for target in target_candidates:
                adapter = get_adapter(target["endpoint_id"])
                endpoint_defaults = plan["endpoint_defaults"]
                defaults = endpoint_defaults.get(target["endpoint_id"], {}) if isinstance(endpoint_defaults, Mapping) else {}
                params = deepcopy(adapter.defaults)
                if isinstance(defaults, Mapping):
                    params.update(defaults)
                params.update(plan["shared_params"])
                params.update(target.get("shared_overrides") or {})
                params.update(target.get("target_overrides") or {})
                lora_scale = params.pop("lora_scale", None)
                for key in axis_keys:
                    axis = axes[key]
                    if axis["name"] == "lora_scale":
                        lora_scale = axis["values"][coordinate[key]]
                    elif axis["name"] not in {"target", "prompt", "case"}:
                        params[axis["name"]] = axis["values"][coordinate[key]]
                input_value = case["input"]
                prompt = str(input_value.get("prompt") or input_value.get("text") or "")
                loras = params.pop("loras", None)
                if loras is None:
                    registered = target.get("registered_lora")
                    loras = [registered] if isinstance(registered, Mapping) and registered.get("path") else []
                if lora_scale is not None:
                    scale = adapter.parse_grid_axis_value("lora_scale", lora_scale)
                    loras = [{**dict(lora), "scale": scale} for lora in loras]
                try:
                    request = adapter.build_request(prompt=prompt, loras=loras, parameters=params, grid_cell=True)
                    status = "admissible"
                    error = None
                except (ValidationError, ValueError) as exc:
                    request = params
                    status = "invalid"
                    error = {"code": "SCHEMA_INVALID", "message": str(exc), "endpoint_id": target["endpoint_id"], "schema_digest": target["schema_digest"]}
                cells.append({
                    "ordinal": ordinal,
                    "coordinate": coordinate.copy(),
                    "case": deepcopy(case),
                    "target": deepcopy(target),
                    "effective_params": request,
                    "request_count": 1,
                    "estimated_cost": "0.000000",
                    "schema_digest": target["schema_digest"],
                    "status": status,
                    "error": error,
                })
                ordinal += 1
    snapshot = deepcopy(plan)
    snapshot["digest"] = digest(snapshot)
    return {"plan": snapshot, "cells": cells, "request_count": len(cells), "estimated_cost": "0.000000", "valid": all(cell["status"] == "admissible" for cell in cells), "schema_errors": [cell["error"] for cell in cells if cell.get("error")]}
