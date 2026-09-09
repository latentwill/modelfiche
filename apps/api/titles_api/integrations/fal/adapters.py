from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, ClassVar


class ValidationError(ValueError):
    pass


IMAGE_PRESETS = {"square_hd", "square", "portrait_4_3", "portrait_16_9", "landscape_4_3", "landscape_16_9"}

GRID_AXIS_BASE_SCHEMA: dict[str, dict[str, Any]] = {
    "prompt": {"label": "Prompt", "type": "string", "parser": "string", "default_values": []},
    "seed": {"label": "Seed", "type": "integer", "parser": "integer", "nullable": True, "default_values": [1, 2]},
    "lora_scale": {"label": "LoRA scale", "type": "number", "parser": "number", "min": 0, "max": 4, "step": 0.1, "default": 1, "default_values": [0.8, 1.0]},
}
GRID_NON_CONFIGURABLE_FIELDS = frozenset({"prompt", "loras", "sync_mode", "num_images"})


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
    compatible_base_model_markers: ClassVar[tuple[str, ...]]
    prompt_max_length: ClassVar[int] = 64_000

    def grid_contract(self) -> dict[str, Any]:
        axis_fields: dict[str, dict[str, Any]] = {}
        for field in sorted(self.allowed_axes):
            schema = deepcopy(self.field_schema.get(field) or GRID_AXIS_BASE_SCHEMA.get(field) or {})
            schema.setdefault("label", field.replace("_", " ").title())
            schema.setdefault("parser", schema.get("type", "string"))
            if field == "prompt":
                schema["max_length"] = self.prompt_max_length
            if field in self.defaults:
                schema.setdefault("default", deepcopy(self.defaults[field]))
            if "default_values" not in schema:
                options = list(schema.get("options") or [])
                if options:
                    schema["default_values"] = options[:2]
                elif "default" in schema and schema["default"] is not None:
                    schema["default_values"] = [deepcopy(schema["default"])]
                else:
                    schema["default_values"] = []
            axis_fields[field] = schema
        fixed_fields = sorted(
            (set(self.allowed_request_fields) - GRID_NON_CONFIGURABLE_FIELDS)
            | ({"lora_scale"} if "lora_scale" in self.allowed_axes else set())
        )
        fixed_schema = {
            field: deepcopy(
                self.field_schema.get(field)
                or GRID_AXIS_BASE_SCHEMA.get(field)
                or {"label": field.replace("_", " ").title(), "type": "string", "parser": "string"}
            )
            for field in fixed_fields
        }
        for field, schema in fixed_schema.items():
            schema.setdefault("label", field.replace("_", " ").title())
            schema.setdefault("parser", schema.get("type", "string"))
            if field in self.defaults:
                schema.setdefault("default", deepcopy(self.defaults[field]))
        preferred_axes = [field for field in ("prompt", "seed", "lora_scale") if field in axis_fields]
        default_axes = (preferred_axes + [field for field in axis_fields if field not in preferred_axes])[:3]
        return {"axis_fields": axis_fields, "default_axes": default_axes, "fixed_fields": fixed_fields, "fixed_field_schema": fixed_schema}

    def parse_grid_axis_value(self, field: str, value: Any) -> Any:
        schema = self.grid_contract()["axis_fields"].get(field)
        if schema is None:
            raise ValidationError(f"{field} is not a supported grid axis for {self.endpoint_id}")
        parser = str(schema.get("parser") or schema.get("type") or "string")
        if parser == "model_version":
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{field} requires a model version id")
            return value.strip()
        if parser == "string":
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"{field} requires a non-empty value")
            parsed: Any = value.strip()
            maximum = schema.get("max_length")
            if maximum is not None and len(parsed) > int(maximum):
                raise ValidationError(f"{field} must be no longer than {maximum} characters")
            return parsed
        if parser == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                return value.strip().lower() == "true"
            raise ValidationError(f"{field} must be true or false")
        if parser in {"integer", "number"}:
            try:
                parsed = int(value) if parser == "integer" else float(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{field} must be {parser}") from exc
            if isinstance(value, bool) or (parser == "integer" and isinstance(value, float) and not value.is_integer()):
                raise ValidationError(f"{field} must be {parser}")
            minimum, maximum = schema.get("min"), schema.get("max")
            if minimum is not None and parsed < minimum or maximum is not None and parsed > maximum:
                raise ValidationError(f"{field} must be from {minimum} to {maximum}")
            return parsed
        if parser == "enum":
            options = list(schema.get("options") or [])
            if value not in options:
                raise ValidationError(f"{field} must be one of: {', '.join(map(str, options))}")
            return value
        if parser == "image_size":
            if isinstance(value, str) and value in set(schema.get("options") or []):
                return value
            if not isinstance(value, dict) or set(value) != {"width", "height"}:
                raise ValidationError(f"{field} must be a supported preset or custom width and height")
            custom = dict(schema.get("custom") or {})
            for dimension in value.values():
                if not isinstance(dimension, int) or isinstance(dimension, bool):
                    raise ValidationError(f"{field} custom dimensions must be integers")
                if dimension < int(custom.get("min", 1)) or dimension > int(custom.get("max", self.image_max)):
                    raise ValidationError(f"{field} custom dimensions are outside endpoint bounds")
                step = int(custom.get("step", 1))
                if step > 1 and dimension % step:
                    raise ValidationError(f"{field} custom dimensions must use increments of {step}")
            return value
        raise ValidationError(f"{field} uses unsupported parser {parser}")

    def supports_base_model(self, base_model: str | None) -> bool:
        normalized = (base_model or "").strip().lower().replace("_", "-")
        return any(marker in normalized for marker in self.compatible_base_model_markers)

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
        if request.get("output_format") not in {"jpeg", "png", "webp"}:
            raise ValidationError("output_format must be jpeg, png, or webp")
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
    compatible_base_model_markers = ("ideogram",)
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
    allowed_axes = frozenset({"prompt", "lora_scale", "seed", "expansion_model", "image_size", "rendering_speed", "acceleration"})
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
        _enum(request, "output_format", {"jpeg", "png"})
        custom = request.get("image_size")
        if isinstance(custom, dict):
            for dimension in custom.values():
                if dimension < 512 or dimension % 16:
                    raise ValidationError("Ideogram custom dimensions must be 512-3840 and multiples of 16")


class Krea2TurboLoraAdapter(EndpointAdapter):
    endpoint_id = "fal-ai/krea-2/turbo/lora"
    display_name = "Krea 2 Turbo LoRA"
    image_max = 14142
    compatible_base_model_markers = ("krea",)
    prompt_max_length = 5000
    defaults = {
        "image_size": "square_hd",
        "num_images": 1,
        "acceleration": "none",
        "enable_prompt_expansion": False,
        "sync_mode": False,
        "enable_safety_checker": True,
        "output_format": "png",
    }
    allowed_axes = frozenset({"prompt", "lora_scale", "seed", "image_size", "acceleration", "enable_prompt_expansion"})
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
        _enum(request, "output_format", {"jpeg", "png"})
        if not isinstance(request.get("enable_prompt_expansion"), bool):
            raise ValidationError("enable_prompt_expansion must be boolean")



class ZImageTurboLoraAdapter(EndpointAdapter):
    endpoint_id = "fal-ai/z-image/turbo/lora"
    display_name = "Z Image Turbo LoRA"
    image_max = 14142
    compatible_base_model_markers = ("z-image", "z image")
    defaults = {
        "image_size": "landscape_4_3",
        "num_images": 1,
        "acceleration": "regular",
        "enable_prompt_expansion": False,
        "num_inference_steps": 8,
        "sync_mode": False,
        "enable_safety_checker": True,
        "output_format": "png",
    }
    allowed_axes = frozenset({"prompt", "lora_scale", "seed", "image_size", "acceleration", "enable_prompt_expansion", "num_inference_steps"})
    allowed_request_fields = frozenset({"prompt", "seed", "image_size", "num_images", "acceleration", "enable_prompt_expansion", "num_inference_steps", "sync_mode", "enable_safety_checker", "output_format", "loras"})
    field_schema = {
        "seed": {"type": "integer", "nullable": True},
        "image_size": {"type": "image_size", "options": sorted(IMAGE_PRESETS), "custom": {"min": 1, "max": 14142, "step": 1}},
        "num_images": {"type": "integer", "min": 1, "max": 4},
        "acceleration": {"type": "enum", "options": ["none", "regular", "high"]},
        "enable_prompt_expansion": {"type": "boolean"},
        "num_inference_steps": {"type": "integer", "min": 1, "max": 8},
        "enable_safety_checker": {"type": "boolean"},
        "output_format": {"type": "enum", "options": ["jpeg", "png", "webp"]},
    }

    def validate(self, request: dict[str, Any]) -> None:
        super().validate(request)
        _enum(request, "acceleration", {"none", "regular", "high"})
        _integer_range(request, "num_inference_steps", 1, 8)
        if not isinstance(request.get("enable_prompt_expansion"), bool):
            raise ValidationError("enable_prompt_expansion must be boolean")


class Flux2Klein9BBaseLoraAdapter(EndpointAdapter):
    endpoint_id = "fal-ai/flux-2/klein/9b/base/lora"
    display_name = "FLUX.2 Klein 9B Base LoRA"
    image_max = 14142
    compatible_base_model_markers = ("klein",)
    defaults = {
        "negative_prompt": "",
        "guidance_scale": 5.0,
        "num_inference_steps": 28,
        "image_size": "landscape_4_3",
        "num_images": 1,
        "acceleration": "regular",
        "sync_mode": False,
        "enable_safety_checker": True,
        "output_format": "png",
    }
    allowed_axes = frozenset({"prompt", "lora_scale", "seed", "negative_prompt", "guidance_scale", "num_inference_steps", "image_size", "acceleration"})
    allowed_request_fields = frozenset({"prompt", "negative_prompt", "guidance_scale", "seed", "num_inference_steps", "image_size", "num_images", "acceleration", "sync_mode", "enable_safety_checker", "output_format", "loras"})
    field_schema = {
        "negative_prompt": {"type": "string"},
        "guidance_scale": {"type": "number", "min": 0, "max": 20, "step": 0.1},
        "seed": {"type": "integer", "nullable": True},
        "num_inference_steps": {"type": "integer", "min": 4, "max": 50},
        "image_size": {"type": "image_size", "options": sorted(IMAGE_PRESETS), "custom": {"min": 1, "max": 14142, "step": 1}},
        "num_images": {"type": "integer", "min": 1, "max": 4},
        "acceleration": {"type": "enum", "options": ["none", "regular", "high"]},
        "enable_safety_checker": {"type": "boolean"},
        "output_format": {"type": "enum", "options": ["jpeg", "png", "webp"]},
    }

    def validate(self, request: dict[str, Any]) -> None:
        super().validate(request)
        _enum(request, "acceleration", {"none", "regular", "high"})
        _integer_range(request, "num_inference_steps", 4, 50)
        _number_range(request, "guidance_scale", 0, 20)
        if not isinstance(request.get("negative_prompt"), str):
            raise ValidationError("negative_prompt must be a string")

def _enum(request: dict[str, Any], field: str, choices: set[str]) -> None:
    if request.get(field) not in choices:
        raise ValidationError(f"{field} must be one of: {', '.join(sorted(choices))}")

def _integer_range(request: dict[str, Any], field: str, minimum: int, maximum: int) -> None:
    value = request.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValidationError(f"{field} must be an integer from {minimum} to {maximum}")


def _number_range(request: dict[str, Any], field: str, minimum: float, maximum: float) -> None:
    value = request.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValidationError(f"{field} must be a number from {minimum} to {maximum}")


_ADAPTERS: dict[str, EndpointAdapter] = {
    adapter.endpoint_id: adapter for adapter in (
        IdeogramV4LoraAdapter(),
        Krea2TurboLoraAdapter(),
        ZImageTurboLoraAdapter(),
        Flux2Klein9BBaseLoraAdapter(),
    )
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
