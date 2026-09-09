"""Add typed training metrics, summaries, and durable run events.

Revision ID: 0017_live_training_telemetry
Revises: 0016_training_launch_foundation
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_live_training_telemetry"
down_revision = "0016_training_launch_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "run_events" in sa.inspect(op.get_bind()).get_table_names():
        return

    with op.batch_alter_table("training_metrics") as batch:
        batch.alter_column("value", existing_type=sa.Float(), nullable=True)
        batch.add_column(sa.Column("value_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("value_type", sa.String(length=16), nullable=False, server_default="number"))
        batch.add_column(sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")))
        batch.add_column(sa.Column("batch_key", sa.String(length=128), nullable=True))
        batch.create_index("ix_training_metrics_batch_key", ["batch_key"])
        batch.create_check_constraint(
            "ck_training_metric_typed_value",
            "(value_type = 'number' AND value IS NOT NULL AND value_text IS NULL) OR "
            "(value_type = 'text' AND value IS NULL AND value_text IS NOT NULL) OR "
            "(value_type = 'null' AND value IS NULL AND value_text IS NULL)",
        )

    op.create_table(
        "run_metric_summaries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("last_step", sa.Integer(), nullable=False),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("last_value_text", sa.Text(), nullable=True),
        sa.Column("value_type", sa.String(length=16), nullable=False),
        sa.Column("minimum", sa.Float(), nullable=True),
        sa.Column("maximum", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "name", name="uq_run_metric_summary"),
    )
    op.create_index("ix_run_metric_summaries_run_id", "run_metric_summaries", ["run_id"])

    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("step", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
        sa.UniqueConstraint("idempotency_key", name="uq_run_event_idempotency"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_index("ix_run_events_type", "run_events", ["type"])
    op.create_index("ix_run_events_step", "run_events", ["step"])
    op.create_index("ix_run_events_occurred_at", "run_events", ["occurred_at"])
    op.create_index("ix_run_events_run_sequence", "run_events", ["run_id", "sequence"])


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_table("run_metric_summaries")
    with op.batch_alter_table("training_metrics") as batch:
        batch.drop_column("batch_key")
        batch.drop_column("ingested_at")
        batch.drop_column("value_type")
        batch.drop_column("value_text")
        batch.alter_column("value", existing_type=sa.Float(), nullable=False)
