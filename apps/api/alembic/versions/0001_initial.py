"""Frozen initial local-first domain schema."""

from alembic import op

from titles_api.storage.baselines import apply_frozen_schema


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_frozen_schema(op.get_bind(), "0001")


def downgrade() -> None:
    raise NotImplementedError("the frozen compatibility baseline is not downgradable")
