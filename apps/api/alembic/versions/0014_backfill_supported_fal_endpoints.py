"""Backfill endpoint registrations for supported uploaded LoRAs.

Revision ID: 0014_backfill_supported_fal_endpoints
Revises: 0013_reclassify_runs_by_trigger
"""
from __future__ import annotations

from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0014_backfill_supported_fal_endpoints"
down_revision = "0013_reclassify_runs_by_trigger"
branch_labels = None
depends_on = None


def _endpoint_for(base_model: str | None) -> str | None:
    normalized = (base_model or "").casefold()
    if "krea-2" in normalized or "krea 2" in normalized:
        return "fal-ai/krea-2/turbo/lora"
    if "ideogram" in normalized:
        return "ideogram/v4/lora"
    return None


def upgrade():
    bind = op.get_bind()
    metadata = sa.MetaData()
    versions = sa.Table("model_versions", metadata, autoload_with=bind)

    for row in bind.execute(sa.select(versions.c.id, versions.c.base_model, versions.c.readiness)).mappings():
        readiness: dict[str, Any] = dict(row["readiness"] or {})
        if not readiness.get("fal_url") or any(readiness.get(key) for key in ("endpoint_id", "endpoint", "fal_endpoint")):
            continue
        endpoint = _endpoint_for(row["base_model"])
        if endpoint is None:
            continue
        readiness["endpoint_id"] = endpoint
        bind.execute(versions.update().where(versions.c.id == row["id"]).values(readiness=readiness))


def downgrade():
    # An endpoint inferred from an immutable base-model family is safe to retain.
    pass
