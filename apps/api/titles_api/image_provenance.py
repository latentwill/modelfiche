from __future__ import annotations

from pathlib import PurePosixPath
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
