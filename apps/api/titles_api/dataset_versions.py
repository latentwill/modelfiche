"""Helpers for immutable dataset version lineage and content identity."""

from hashlib import sha256
import json
from typing import Any, Iterable


def content_digest(version: Any, items: Iterable[Any]) -> str:
    """Return the stable digest for a version's defining metadata and items.

    Items are ordered by their persisted position, then by asset identity so the
    digest is independent of query order while retaining the dataset's order.
    """
    ordered_items = sorted(items, key=lambda item: (item.position, item.asset_id, item.id))
    payload = {
        "version": {
            "name": version.name,
            "source_uri": version.source_uri,
            "caption_format": version.caption_format,
            "trigger_words": list(version.trigger_words or []),
        },
        "items": [
            {
                "asset_id": item.asset_id,
                "caption": item.caption,
                "caption_format": item.caption_format,
                "included": item.included,
                "tags": list(item.tags or []),
                "position": item.position,
            }
            for item in ordered_items
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()
