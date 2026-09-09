from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable

from .adapters import LoraInput, get_adapter


@dataclass(frozen=True, slots=True)
class EvalTask:
    endpoint_id: str
    prompt_id: str
    prompt: str
    checkpoint_id: str
    request: dict[str, Any]
    coordinates: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GridAxis:
    field: str
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class GridDefinition:
    endpoint_id: str
    row_axis: GridAxis
    column_axis: GridAxis
    base_parameters: dict[str, Any]


class GridPlanner:
    def __init__(self, resolve_checkpoint: Callable[[str | int], tuple[str, str]]):
        """resolve_checkpoint(step) returns (checkpoint_id, FAL-accessible path)."""
        self.resolve_checkpoint = resolve_checkpoint

    def plan(
        self,
        definition: GridDefinition,
        *,
        prompt_id: str,
        prompt: str,
        checkpoint_id: str,
        lora_path: str,
        lora_scale: float = 1.0,
    ) -> list[EvalTask]:
        adapter = get_adapter(definition.endpoint_id)
        fields = {definition.row_axis.field, definition.column_axis.field}
        unsupported = fields - (adapter.allowed_axes | {"checkpoint_step", "model"})
        if unsupported:
            raise ValueError(f"unsupported grid axes for {adapter.endpoint_id}: {', '.join(sorted(unsupported))}")
        if len(fields) != 2:
            raise ValueError("row and column axes must be different")
        tasks: list[EvalTask] = []
        for row_value, column_value in product(definition.row_axis.values, definition.column_axis.values):
            parameters = dict(definition.base_parameters)
            current_prompt = prompt
            current_prompt_id = prompt_id
            current_checkpoint = checkpoint_id
            current_path = lora_path
            current_scale = lora_scale
            coordinates = {definition.row_axis.field: row_value, definition.column_axis.field: column_value}
            for axis_field, value in coordinates.items():
                if axis_field == "checkpoint_step":
                    current_checkpoint, current_path = self.resolve_checkpoint(value)
                elif axis_field == "lora_scale":
                    current_scale = float(value)
                elif axis_field == "prompt":
                    if not isinstance(value, dict) or "id" not in value or "text" not in value:
                        raise ValueError("prompt axis values require id and text")
                    current_prompt_id, current_prompt = str(value["id"]), str(value["text"])
                elif axis_field == "model":
                    if not isinstance(value, dict) or "checkpoint_id" not in value or "path" not in value:
                        raise ValueError("model axis values require checkpoint_id and path")
                    current_checkpoint, current_path = str(value["checkpoint_id"]), str(value["path"])
                else:
                    parameters[axis_field] = value
            request = adapter.build_request(
                prompt=current_prompt,
                loras=[LoraInput(current_path, current_scale)],
                parameters=parameters,
                grid_cell=True,
            )
            tasks.append(EvalTask(adapter.endpoint_id, current_prompt_id, current_prompt, current_checkpoint, request, coordinates))
        return tasks
