from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, ClassVar


class ValidationError(ValueError):
    pass


IMAGE_PRESETS = {"square_hd", "square", "portrait_4_3", "portrait_16_9", "landscape_4_3", "landscape_16_9"}


@dataclass(frozen=True, slots=True)
class LoraInput:
    path: str
    scale: float = 1.0


class EndpointAdapter:
    endpoint_id: ClassVar[str]
    display_name: ClassVar[str]
    defaults: ClassVar[dict[str, Any]]
    field_schema: ClassVar[dict[str, dict[str, Any]]]
    allowed_axes: ClassVar[frozenset[str]]
    allowed_request_fields: ClassVar[frozenset[str]]
    image_max: ClassVar[int]

    def build_request(
        self,
        *,
        prompt: str,
        loras: list[LoraInput | dict[str, Any]],
        parameters: dict[str, Any] | None = None,
        grid_cell: bool = False,
    ) -> dict[str, Any]:
        request = deepcopy(self.defaults)
        request.update(parameters or {})
        request["prompt"] = prompt
        request["loras"] = [item if isinstance(item, dict) else {"path": item.path, "scale": item.scale} for item in loras]
        request["sync_mode"] = False
        if grid_cell:
            request["num_images"] = 1
        self.validate(request)
        return request

    def validate(self, request: dict[str, Any]) -> None:
        unexpected = set(request) - self.allowed_request_fields
        if unexpected:
            raise ValidationError(f"unsupported fields for {self.endpoint_id}: {', '.join(sorted(unexpected))}")
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValidationError("prompt is required")
        self._validate_common(request)

    def _validate_common(self, request: dict[str, Any]) -> None:
        count = request.get("num_images", 1)
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 4:
            raise ValidationError("num_images must be an integer from 1 to 4")
        seed = request.get("seed")
        if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
            raise ValidationError("seed must be an integer or null")
        if request.get("sync_mode") is not False:
            raise ValidationError("sync_mode must remain false for durable provider history")
        if request.get("output_format") not in {"jpeg", "png"}:
            raise ValidationError("output_format must be jpeg or png")
        for name in ("sync_mode", "enable_safety_checker"):
            if not isinstance(request.get(name), bool):
                raise ValidationError(f"{name} must be boolean")
        loras = request.get("loras", [])
        if not isinstance(loras, list) or len(loras) > 3:
            raise ValidationError("loras must contain at most three items")
        for lora in loras:
            if not isinstance(lora, dict) or not isinstance(lora.get("path"), str) or not lora["path"]:
                raise ValidationError("each LoRA requires a path")
            scale = lora.get("scale", 1)
            if not isinstance(scale, (int, float)) or isinstance(scale, bool) or not 0 <= scale <= 4:
                raise ValidationError("LoRA scale must be between 0 and 4")
        self._validate_image_size(request.get("image_size", "square_hd"))

    def _validate_image_size(self, image_size: Any) -> None:
        if isinstance(image_size, str):
            if image_size not in IMAGE_PRESETS:
                raise ValidationError(f"unsupported image_size preset: {image_size}")
            return
        if not isinstance(image_size, dict) or set(image_size) != {"width", "height"}:
            raise ValidationError("custom image_size requires width and height")
        for value in image_size.values():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > self.image_max:
                raise ValidationError(f"custom dimensions must be positive integers no greater than {self.image_max}")


class IdeogramV4LoraAdapter(EndpointAdapter):
    endpoint_id = "ideogram/v4/lora"
    display_name = "Ideogram 4 LoRA"
    image_max = 3840
    defaults = {
        "expansion_model": "Medium",
        "image_size": "square_hd",
        "rendering_speed": "BALANCED",
        "acceleration": "none",
        "num_images": 1,
        "sync_mode": False,
        "enable_safety_checker": True,
        "output_format": "jpeg",
    }
    allowed_axes = frozenset({"prompt", "checkpoint_step", "lora_scale", "seed", "model", "expansion_model", "image_size", "rendering_speed", "acceleration"})
    allowed_request_fields = frozenset({"prompt", "expansion_model", "image_size", "rendering_speed", "acceleration", "num_images", "seed", "sync_mode", "enable_safety_checker", "output_format", "loras"})
    field_schema = {
        "expansion_model": {"type": "enum", "options": ["None", "Medium", "Large"]},
        "image_size": {"type": "image_size", "options": sorted(IMAGE_PRESETS), "custom": {"min": 512, "max": 3840, "step": 16}},
        "rendering_speed": {"type": "enum", "options": ["TURBO", "BALANCED", "QUALITY"]},
        "acceleration": {"type": "enum", "options": ["none", "low", "regular", "high"]},
        "num_images": {"type": "integer", "min": 1, "max": 4},
        "seed": {"type": "integer", "nullable": True},
        "enable_safety_checker": {"type": "boolean"},
        "output_format": {"type": "enum", "options": ["jpeg", "png"]},
    }

    def validate(self, request: dict[str, Any]) -> None:
        super().validate(request)
        _enum(request, "expansion_model", {"None", "Medium", "Large"})
        _enum(request, "rendering_speed", {"TURBO", "BALANCED", "QUALITY"})
        _enum(request, "acceleration", {"none", "low", "regular", "high"})
        custom = request.get("image_size")
        if isinstance(custom, dict):
            for dimension in custom.values():
                if dimension < 512 or dimension % 16:
                    raise ValidationError("Ideogram custom dimensions must be 512-3840 and multiples of 16")


class Krea2TurboLoraAdapter(EndpointAdapter):
    endpoint_id = "fal-ai/krea-2/turbo/lora"
    display_name = "Krea 2 Turbo LoRA"
    image_max = 14142
    defaults = {
        "image_size": "square_hd",
        "num_images": 1,
        "acceleration": "none",
        "enable_prompt_expansion": False,
        "sync_mode": False,
        "enable_safety_checker": True,
        "output_format": "png",
    }
    allowed_axes = frozenset({"prompt", "checkpoint_step", "lora_scale", "seed", "model", "image_size", "acceleration", "enable_prompt_expansion"})
    allowed_request_fields = frozenset({"prompt", "seed", "image_size", "num_images", "acceleration", "enable_prompt_expansion", "sync_mode", "enable_safety_checker", "output_format", "loras"})
    field_schema = {
        "seed": {"type": "integer", "nullable": True},
        "image_size": {"type": "image_size", "options": sorted(IMAGE_PRESETS), "custom": {"min": 1, "max": 14142, "step": 1}},
        "num_images": {"type": "integer", "min": 1, "max": 4},
        "acceleration": {"type": "enum", "options": ["none", "regular"]},
        "enable_prompt_expansion": {"type": "boolean"},
        "enable_safety_checker": {"type": "boolean"},
        "output_format": {"type": "enum", "options": ["jpeg", "png"]},
    }

    def validate(self, request: dict[str, Any]) -> None:
        super().validate(request)
        if len(request["prompt"]) > 5000:
            raise ValidationError("Krea prompt cannot exceed 5000 characters")
        _enum(request, "acceleration", {"none", "regular"})
        if not isinstance(request.get("enable_prompt_expansion"), bool):
            raise ValidationError("enable_prompt_expansion must be boolean")


def _enum(request: dict[str, Any], field: str, choices: set[str]) -> None:
    if request.get(field) not in choices:
        raise ValidationError(f"{field} must be one of: {', '.join(sorted(choices))}")


_ADAPTERS: dict[str, EndpointAdapter] = {
    adapter.endpoint_id: adapter for adapter in (IdeogramV4LoraAdapter(), Krea2TurboLoraAdapter())
}


def get_adapter(endpoint_id: str) -> EndpointAdapter:
    try:
        return _ADAPTERS[endpoint_id]
    except KeyError as exc:
        raise KeyError(f"unsupported FAL endpoint: {endpoint_id}") from exc


def list_adapters() -> tuple[EndpointAdapter, ...]:
    return tuple(_ADAPTERS.values())


def normalize_parameters(parameters: dict[str, Any], *, grid_cell: bool = False) -> dict[str, Any]:
    """Validate and complete the closed, persisted FAL parameter contract."""
    raw = dict(parameters)
    endpoint_id = raw.pop("endpoint_id", None)
    if not isinstance(endpoint_id, str):
        raise ValidationError("endpoint_id is required")

    lora_scale = raw.pop("lora_scale", 1.0)
    if not isinstance(lora_scale, (int, float)) or isinstance(lora_scale, bool) or not 0 <= lora_scale <= 4:
        raise ValidationError("lora_scale must be a number from 0 to 4")

    adapter = get_adapter(endpoint_id)
    request = adapter.build_request(
        prompt="FAL admission parameter validation",
        loras=[LoraInput("https://admission.invalid/checkpoint", float(lora_scale))],
        parameters=raw,
        grid_cell=grid_cell,
    )
    request.setdefault("seed", None)
    request.pop("prompt")
    request.pop("loras")
    request.pop("sync_mode")
    return {"endpoint_id": endpoint_id, **request, "lora_scale": float(lora_scale)}
