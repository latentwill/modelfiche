"""Align the static content blob table with the timestamped ORM model."""

from alembic import op
import sqlalchemy as sa


revision = "0005_content_blob_timestamps"
down_revision = "0004_training_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_blobs", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("content_blobs", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE content_blobs SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
    op.execute("UPDATE content_blobs SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL")


def downgrade() -> None:
    op.drop_column("content_blobs", "updated_at")
    op.drop_column("content_blobs", "created_at")
