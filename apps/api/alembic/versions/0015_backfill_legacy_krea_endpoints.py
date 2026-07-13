"""Register supported legacy Krea uploads by model name.

Revision ID: 0015_backfill_legacy_krea_endpoints
Revises: 0014_backfill_supported_fal_endpoints
"""
from __future__ import annotations

from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0015_backfill_legacy_krea_endpoints"
down_revision = "0014_backfill_supported_fal_endpoints"
branch_labels = None
depends_on = None


def _endpoint_for(*evidence: str | None) -> str | None:
    normalized = " ".join(value or "" for value in evidence).casefold()
    if "krea-2" in normalized or "krea 2" in normalized or "krea2" in normalized:
        return "fal-ai/krea-2/turbo/lora"
    if "ideogram" in normalized:
        return "ideogram/v4/lora"
    return None


def upgrade():
    bind = op.get_bind()
    metadata = sa.MetaData()
    versions = sa.Table("model_versions", metadata, autoload_with=bind)
    models = sa.Table("models", metadata, autoload_with=bind)

    rows = bind.execute(
        sa.select(versions.c.id, versions.c.base_model, versions.c.readiness, models.c.name.label("model_name"))
        .join(models, models.c.id == versions.c.model_id)
    ).mappings()
    for row in rows:
        readiness: dict[str, Any] = dict(row["readiness"] or {})
        if not readiness.get("fal_url") or any(readiness.get(key) for key in ("endpoint_id", "endpoint", "fal_endpoint")):
            continue
        endpoint = _endpoint_for(row["base_model"], row["model_name"])
        if endpoint is None:
            continue
        readiness["endpoint_id"] = endpoint
        bind.execute(versions.update().where(versions.c.id == row["id"]).values(readiness=readiness))


def downgrade():
    # The model family is stable provenance for this provider registration.
    pass
