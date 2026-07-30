"""Add typed conditioning artifact metadata to model versions.

Revision ID: 0024_embedding_artifacts
Revises: 0023_modern_times_merge_reconciliation
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0024_embedding_artifacts"
down_revision = "0023_modern_times_merge_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_versions") as batch:
        batch.add_column(sa.Column("artifact_type", sa.String(length=32), nullable=False, server_default="lora"))
        batch.add_column(sa.Column("artifact_format", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("method", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("compatibility", sa.JSON(), nullable=False, server_default="{}"))
        batch.create_index("ix_model_versions_artifact_type", ["artifact_type"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("model_versions") as batch:
        batch.drop_index("ix_model_versions_artifact_type")
        batch.drop_column("compatibility")
        batch.drop_column("method")
        batch.drop_column("artifact_format")
        batch.drop_column("artifact_type")
