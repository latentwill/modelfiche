from __future__ import annotations

from pathlib import PurePosixPath
import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import UUID


def fal_generated_at(raw: dict[str, Any]) -> datetime | None:
    """Return FAL completion time in authoritative provider order."""
    history = raw.get("_history") if isinstance(raw.get("_history"), dict) else raw.get("history")
    candidates = [
        *(history.get(key) for key in ("ended_at", "sent_at") if isinstance(history, dict)),
        raw.get("generated_at"),
    ]
    for value in candidates:
        if not value:
            continue
        try:
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            if isinstance(value, (int, float)):
                seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
                return datetime.fromtimestamp(seconds, timezone.utc)
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            continue
    try:
        identifier = UUID(str(raw.get("_request_id") or raw.get("fal_request_id")))
        if identifier.version == 7:
            return datetime.fromtimestamp((identifier.int >> 80) / 1000, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def normalize_fal_image_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize stable FAL request evidence without claiming ambiguous DB lineage."""
    if str(raw.get("provider", "")).lower() != "fal" and "fal_request_input" not in raw:
        return {}
    request = raw.get("fal_request_input")
    if not isinstance(request, dict):
        request = {}
    endpoint = str(raw.get("endpoint") or request.get("endpoint") or "")
    loras = request.get("loras") if isinstance(request.get("loras"), list) else []
    lora = next((item for item in loras if isinstance(item, dict) and item.get("path")), None)
    path = str(lora.get("path")) if lora else ""
    filename = PurePosixPath(urlparse(path).path).name if path else ""
    stem = re.sub(r"\.safetensors$", "", filename, flags=re.IGNORECASE)
    stem = re.sub(r"^[A-Za-z0-9-]{12,}_", "", stem)
    step_match = re.search(r"(?:[-_](?:fal|step)[-_]?|[-_])(\d{3,})(?:\D|$)", stem, re.IGNORECASE)
    step = int(step_match.group(1)) if step_match else None
    model_name = re.sub(r"(?:[-_](?:fal|step)[-_]?\d{3,}|[-_]0{2,}\d{3,})$", "", stem, flags=re.IGNORECASE).strip("-_")
    identity_tokens = {token for token in re.split(r"[^a-z0-9]+", model_name.lower()) if token}
    if not ({"vcribb", "kzapata"} & identity_tokens):
        model_name = ""
    endpoint_lower = endpoint.lower()
    base_model = (
        "krea/Krea-2-Raw" if "krea" in endpoint_lower
        else "ideogram-ai/ideogram-4-fp8" if "ideogram" in endpoint_lower
        else "qwen-image" if "qwen" in endpoint_lower
        else "black-forest-labs/FLUX" if "flux" in endpoint_lower
        else None
    )
    normalized = {
        "generation_settings": request or None,
        "endpoint": endpoint or None,
        "base_model": base_model,
        "inferred_model_name": model_name or None,
        "step": step,
        "lora": ({"path": path, "scale": lora.get("scale")} if lora else None),
        "lora_scale": lora.get("scale") if lora else None,
        "model_attribution": "inferred_from_fal_lora_filename" if model_name else None,
    }
    return {key: value for key, value in normalized.items() if value is not None}
 
def _decode_exif_text(value: Any) -> Any:
    if not isinstance(value, bytes):
        return value
    try:
        if value.startswith(b"UNICODE\x00"):
            return value[8:].decode("utf-16", "replace").rstrip("\x00")
        if value.startswith(b"ASCII\x00\x00\x00"):
            return value[8:].decode("ascii", "replace").rstrip("\x00")
        return value.decode("utf-8", "replace").rstrip("\x00")
    except (UnicodeError, ValueError):
        return value.decode("utf-8", "replace").rstrip("\x00")
 
def _metadata_value(value: Any, *, depth: int = 0) -> Any:
    """Convert Pillow metadata values into JSON-safe, bounded primitives."""
    if depth >= 8:
        return None
    value = _decode_exif_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return value[:1_048_576]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {
            str(key)[:256]: _metadata_value(item, depth=depth + 1)
            for key, item in list(value.items())[:256]
        }
    if isinstance(value, (tuple, list)):
        return [_metadata_value(item, depth=depth + 1) for item in value[:64]]
    return str(value)[:16_384]


def _decode_embedded_value(value: Any) -> Any:
    value = _metadata_value(value)
    if not isinstance(value, str):
        return value
    candidate = value.strip()
    if candidate[:1] not in {"{", "["}:
        return value
    try:
        import json

        parsed = json.loads(candidate)
    except (TypeError, ValueError, RecursionError):
        return value
    return _metadata_value(parsed)


def extract_image_metadata(data: bytes) -> dict[str, Any]:
    """Extract stable image facts and embedded generation evidence.

    This intentionally accepts bytes rather than a path so local imports,
    hydrated S3 objects, and archive-expanded objects share one parser. Invalid
    or unsupported bytes return an empty mapping; callers can still persist the
    asset and report the import independently.
    """
    if not data:
        return {}
    try:
        from io import BytesIO

        from PIL import Image, ExifTags

        with Image.open(BytesIO(data)) as image:
            result: dict[str, Any] = {
                "width": int(image.width),
                "height": int(image.height),
                "color_mode": str(image.mode),
            }
            exif_summary: dict[str, Any] = {}
            orientation: Any = None
            exif: Any = {}
            try:
                exif = image.getexif()
                values = list(exif.items())
                try:
                    exif_ifd = getattr(ExifTags.IFD, "Exif", 34665)
                    nested = exif.get_ifd(exif_ifd)
                    values.extend(nested.items())
                except (AttributeError, KeyError, TypeError, ValueError, IndexError, OSError):
                    pass
                for tag_id, value in values:
                    tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                    if tag_name in {
                        "DateTime",
                        "DateTimeOriginal",
                        "DateTimeDigitized",
                        "Make",
                        "Model",
                        "Software",
                        "Artist",
                        "Copyright",
                        "ImageDescription",
                        "Orientation",
                        "UserComment",
                    }:
                        exif_summary[tag_name] = _metadata_value(value)
                orientation = exif.get(274)
            except (AttributeError, KeyError, TypeError, ValueError, IndexError, UnicodeError, OSError):
                # EXIF is optional and occasionally malformed; preserve the
                # valid dimensions and container metadata in that case.
                exif = {}
            if isinstance(orientation, int) and 1 <= orientation <= 8:
                result["orientation"] = orientation
            if exif_summary:
                result["exif"] = exif_summary

            embedded: dict[str, Any] = {}
            for key, value in image.info.items():
                normalized = str(key).strip().casefold()
                if normalized in {
                    "parameters",
                    "prompt",
                    "workflow",
                    "comfy workflow",
                    "negative prompt",
                    "description",
                    "comment",
                    "software",
                    "source",
                }:
                    embedded[str(key)] = _decode_embedded_value(value)
            for tag_name in ("UserComment", "ImageDescription"):
                value = exif_summary.get(tag_name)
                if value:
                    embedded[f"exif_{tag_name}"] = _decode_embedded_value(value)
            if embedded:
                result["embedded_metadata"] = embedded
                generation_metadata = {
                    key: value
                    for key, value in embedded.items()
                    if str(key).casefold()
                    in {
                        "parameters",
                        "prompt",
                        "workflow",
                        "comfy workflow",
                        "negative prompt",
                        "exif_usercomment",
                    }
                }
                if generation_metadata:
                    result["generation_metadata"] = generation_metadata

            description = exif_summary.get("ImageDescription")
            if not isinstance(description, str) or not description.strip():
                description = next(
                    (
                        value
                        for key, value in embedded.items()
                        if str(key).casefold() in {"description", "comment"}
                        and isinstance(value, str)
                        and value.strip()
                    ),
                    None,
                )
            if isinstance(description, str) and description.strip():
                result["caption"] = description.strip()
            return result
    except (OSError, ValueError, TypeError):
        return {}
