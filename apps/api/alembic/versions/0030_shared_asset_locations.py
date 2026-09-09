"""Allow multiple assets to reference the same physical object revision.

Revision ID: 0030_shared_asset_locations
Revises: 0029_asset_location_content_deduplication
"""
from __future__ import annotations

from alembic import op


revision = "0030_shared_asset_locations"
down_revision = "0029_asset_location_content_deduplication"
branch_labels = None
depends_on = None


_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
_LEGACY_CONSTRAINT = "uq_asset_locations_provider"


def upgrade() -> None:
    with op.batch_alter_table(
        "asset_locations",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(_LEGACY_CONSTRAINT, type_="unique")


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.exec_driver_sql(
        """
        SELECT provider, bucket, object_key, etag
        FROM asset_locations
        GROUP BY provider, bucket, object_key, etag
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot restore physical-location ownership uniqueness while "
            "one object revision is shared by multiple assets"
        )
    with op.batch_alter_table(
        "asset_locations",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.create_unique_constraint(
            _LEGACY_CONSTRAINT,
            ["provider", "bucket", "object_key", "etag"],
        )
