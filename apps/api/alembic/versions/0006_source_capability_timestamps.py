"""Align source capability persistence with the timestamped ORM model."""

from alembic import op
import sqlalchemy as sa


revision = "0006_source_capability_timestamps"
down_revision = "0005_content_blob_timestamps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_capabilities", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE source_capabilities SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    op.drop_column("source_capabilities", "updated_at")
