"""Persist canonical SAMPLE and EVAL origin timestamps."""

from alembic import op
import sqlalchemy as sa

revision = "0007_asset_origin_timestamps"
down_revision = "0006_source_capability_timestamps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("samples", sa.Column("modified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("eval_outputs", sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("""
        UPDATE samples SET modified_at = (
          SELECT MAX(asset_locations.modified_at) FROM asset_locations
          WHERE asset_locations.asset_id = samples.asset_id
        )
    """)
    op.execute("""
        UPDATE eval_outputs SET generated_at = COALESCE(
          json_extract(provider_metadata, '$.response._history.ended_at'),
          json_extract(provider_metadata, '$.response._history.sent_at'),
          json_extract(provider_metadata, '$.response.generated_at'),
          json_extract(provider_metadata, '$.generated_at'),
          created_at
        )
    """)
    op.create_index("ix_samples_modified_at", "samples", ["modified_at"])
    op.create_index("ix_eval_outputs_generated_at", "eval_outputs", ["generated_at"])


def downgrade() -> None:
    op.drop_index("ix_eval_outputs_generated_at", table_name="eval_outputs")
    op.drop_index("ix_samples_modified_at", table_name="samples")
    op.drop_column("eval_outputs", "generated_at")
    op.drop_column("samples", "modified_at")
