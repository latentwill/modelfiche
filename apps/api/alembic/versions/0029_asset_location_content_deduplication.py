"""Allow one content-addressed object to serve multiple assets.

Revision ID: 0029_asset_location_content_deduplication
Revises: 0028_training_run_placements
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0029_asset_location_content_deduplication"
down_revision = "0028_training_run_placements"
branch_labels = None
depends_on = None


_INDEX = "uq_asset_locations_source_revision"
_PREDICATE = "source_id IS NOT NULL AND source_revision_fingerprint IS NOT NULL"


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="asset_locations")
    op.create_index(
        _INDEX,
        "asset_locations",
        ["asset_id", "source_id", "object_key", "source_revision_fingerprint"],
        unique=True,
        sqlite_where=sa.text(_PREDICATE),
        postgresql_where=sa.text(_PREDICATE),
    )


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.exec_driver_sql(
        """
        SELECT workspace_id, source_id, object_key, source_revision_fingerprint
        FROM asset_locations
        WHERE source_id IS NOT NULL AND object_key IS NOT NULL
        GROUP BY workspace_id, source_id, object_key, source_revision_fingerprint
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot restore workspace-wide asset-location uniqueness while "
            "content-addressed objects are shared by multiple assets"
        )
    op.drop_index(_INDEX, table_name="asset_locations")
    op.create_index(
        _INDEX,
        "asset_locations",
        ["workspace_id", "source_id", "object_key", "source_revision_fingerprint"],
        unique=True,
        sqlite_where=sa.text(_PREDICATE),
        postgresql_where=sa.text(_PREDICATE),
    )
