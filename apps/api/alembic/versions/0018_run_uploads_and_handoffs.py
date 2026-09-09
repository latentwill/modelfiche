"""Add W&B upload correlation and generation-fenced artifact handoffs.

Revision ID: 0018_run_uploads_and_handoffs
Revises: 0017_live_training_telemetry
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_run_uploads_and_handoffs"
down_revision = "0017_live_training_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "run_uploads" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table(
        "run_uploads",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("metric_name", sa.String(length=120), nullable=True),
        sa.Column("step", sa.Integer(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("expected_size", sa.Integer(), nullable=True),
        sa.Column("mime_type", sa.String(length=200), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("presign_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("version_id", sa.String(length=1024), nullable=True),
        sa.Column("asset_id", sa.String(length=36), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("run_id", "relative_path", name="uq_run_upload_path"),
        sa.UniqueConstraint("object_key", name="uq_run_upload_object_key"),
    )
    op.create_index("ix_run_uploads_run_id", "run_uploads", ["run_id"])
    op.create_index("ix_run_uploads_kind", "run_uploads", ["kind"])
    op.create_index("ix_run_uploads_step", "run_uploads", ["step"])
    op.create_index("ix_run_uploads_state", "run_uploads", ["state"])
    op.create_index("ix_run_uploads_asset_id", "run_uploads", ["asset_id"])
    op.create_index("ix_run_uploads_run_state", "run_uploads", ["run_id", "state"])

    op.create_table(
        "run_artifact_handoffs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("manifest_key", sa.String(length=1024), nullable=False),
        sa.Column("manifest_etag", sa.String(length=255), nullable=True),
        sa.Column("manifest_version_id", sa.String(length=1024), nullable=True),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="observed"),
        sa.Column("source_fingerprint", sa.String(length=96), nullable=False),
        sa.Column("checkpoint_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verified_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("imported_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "generation", name="uq_run_artifact_handoff_generation"),
        sa.UniqueConstraint("run_id", "manifest_version_id", name="uq_run_artifact_handoff_manifest_version"),
    )
    op.create_index("ix_run_artifact_handoffs_run_id", "run_artifact_handoffs", ["run_id"])
    op.create_index("ix_run_artifact_handoffs_state", "run_artifact_handoffs", ["state"])


def downgrade() -> None:
    op.drop_table("run_artifact_handoffs")
    op.drop_table("run_uploads")
