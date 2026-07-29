"""Helpers for immutable dataset version lineage and content identity."""

from hashlib import sha256
import json
from typing import Any, Iterable


def content_digest(
    version: Any,
    items: Iterable[Any],
    subsets: Iterable[Any] = (),
    memberships: Iterable[Any] = (),
) -> str:
    """Return the stable digest for a version's metadata, items, and composition."""
    ordered_items = sorted(items, key=lambda item: (item.position, item.asset_id))
    ordered_subsets = sorted(subsets, key=lambda subset: (subset.position, subset.key))
    subset_keys = {subset.id: subset.key for subset in ordered_subsets}
    item_assets = {item.id: item.asset_id for item in ordered_items}
    ordered_memberships = sorted(
        memberships,
        key=lambda membership: (
            subset_keys.get(membership.subset_id, membership.subset_id),
            membership.position,
            item_assets.get(membership.dataset_item_id, membership.dataset_item_id),
        ),
    )
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
        "subsets": [
            {
                "key": subset.key,
                "name": subset.name,
                "description": subset.description,
                "role": subset.role,
                "parent_subset": subset_keys.get(subset.parent_subset_id) if subset.parent_subset_id else None,
                "color_token": subset.color_token,
                "position": subset.position,
            }
            for subset in ordered_subsets
        ],
        "memberships": [
            {
                "subset": subset_keys.get(membership.subset_id, membership.subset_id),
                "asset_id": item_assets.get(membership.dataset_item_id, membership.dataset_item_id),
                "membership_role": membership.membership_role,
                "position": membership.position,
            }
            for membership in ordered_memberships
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()
