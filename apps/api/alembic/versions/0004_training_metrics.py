"""Persist scalar training metrics imported from run artifacts."""

from alembic import op
import sqlalchemy as sa


revision = "0004_training_metrics"
down_revision = "0003_flexible_image_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_metrics",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("wall_time", sa.Float(), nullable=True),
        sa.Column("source_key", sa.String(length=1024), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "step", "name"),
    )
    op.create_index("ix_training_metrics_run_id", "training_metrics", ["run_id"])
    op.create_index(
        "ix_training_metrics_run_name_step",
        "training_metrics",
        ["run_id", "name", "step"],
    )

def downgrade() -> None:
    op.drop_index("ix_training_metrics_run_name_step", table_name="training_metrics")
    op.drop_index("ix_training_metrics_run_id", table_name="training_metrics")
    op.drop_table("training_metrics")
