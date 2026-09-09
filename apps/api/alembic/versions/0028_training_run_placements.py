"""Add repairable training-run placement history.

Revision ID: 0028_training_run_placements
Revises: 0027_review_subject_created_at_indexes
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0028_training_run_placements"
down_revision = "0027_review_subject_created_at_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "placement_epoch" not in {column["name"] for column in inspector.get_columns("training_runs")}:
        op.add_column(
            "training_runs",
            sa.Column("placement_epoch", sa.Integer(), nullable=False, server_default="1"),
        )
    if "training_run_placements" in inspector.get_table_names():
        return
    op.create_table(
        "training_run_placements",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("previous_workspace_id", sa.String(length=36), nullable=True),
        sa.Column("previous_project_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("profile_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["previous_workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["previous_project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["profile_id"], ["user_profiles.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("run_id", "epoch", name="uq_training_run_placement_epoch"),
    )
    op.create_index("ix_training_run_placements_run_id", "training_run_placements", ["run_id"])
    op.create_index("ix_training_run_placements_workspace_id", "training_run_placements", ["workspace_id"])
    op.create_index("ix_training_run_placements_project_id", "training_run_placements", ["project_id"])


def downgrade() -> None:
    op.drop_table("training_run_placements")
    op.drop_column("training_runs", "placement_epoch")
