"""Add explicit training-run archival state.

Revision ID: 0019_training_run_archival
Revises: 0018_run_uploads_and_handoffs
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0019_training_run_archival"
down_revision = "0018_run_uploads_and_handoffs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("training_runs")}
    if "archived_at" in columns:
        return
    with op.batch_alter_table("training_runs") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_training_runs_archived_at", ["archived_at"])


def downgrade() -> None:
    with op.batch_alter_table("training_runs") as batch:
        batch.drop_index("ix_training_runs_archived_at")
        batch.drop_column("archived_at")
