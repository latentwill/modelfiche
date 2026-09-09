"""Add authoritative worker process heartbeats.

Revision ID: 0020_worker_heartbeats
Revises: 0019_training_run_archival
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0020_worker_heartbeats"
down_revision = "0019_training_run_archival"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "worker_heartbeats" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index("ix_worker_heartbeats_state", "worker_heartbeats", ["state"])
    op.create_index("ix_worker_heartbeats_heartbeat_at", "worker_heartbeats", ["heartbeat_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_heartbeats_heartbeat_at", table_name="worker_heartbeats")
    op.drop_index("ix_worker_heartbeats_state", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
