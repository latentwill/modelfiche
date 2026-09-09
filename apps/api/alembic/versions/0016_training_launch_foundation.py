"""Add immutable launch identity and asymmetric W&B credentials.

Revision ID: 0016_training_launch_foundation
Revises: 0015_backfill_legacy_krea_endpoints
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_training_launch_foundation"
down_revision = "0015_backfill_legacy_krea_endpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "training_launches" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.add_column("dataset_versions", sa.Column("parent_version_id", sa.String(length=36), nullable=True))
    op.add_column(
        "dataset_versions",
        sa.Column("lineage_kind", sa.String(length=32), nullable=False, server_default="initial"),
    )
    op.add_column("dataset_versions", sa.Column("content_digest", sa.String(length=64), nullable=True))
    op.create_index("ix_dataset_versions_parent_version_id", "dataset_versions", ["parent_version_id"])
    op.create_index("ix_dataset_versions_lineage_kind", "dataset_versions", ["lineage_kind"])
    op.create_index("ix_dataset_versions_content_digest", "dataset_versions", ["content_digest"])
    with op.batch_alter_table("dataset_versions") as batch:
        batch.create_foreign_key(
            "fk_dataset_versions_parent_version_id",
            "dataset_versions",
            ["parent_version_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("training_runs") as batch:
        batch.add_column(sa.Column("wandb_run_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("wandb_project", sa.String(length=240), nullable=True))
        batch.add_column(sa.Column("wandb_display_name", sa.String(length=240), nullable=True))
        batch.add_column(sa.Column("wandb_sdk_version", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("wandb_protocol_revision", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("current_step", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("exit_code", sa.Integer(), nullable=True))
        batch.create_unique_constraint("uq_training_runs_wandb_run_id", ["wandb_run_id"])
        batch.create_index("ix_training_runs_wandb_run_id", ["wandb_run_id"])
        batch.create_index("ix_training_runs_wandb_project", ["wandb_project"])
        batch.create_index("ix_training_runs_current_step", ["current_step"])
        batch.create_index("ix_training_runs_last_event_at", ["last_event_at"])

    op.create_table(
        "wandb_ingest_credentials",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("alias", sa.String(length=120), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False, server_default="ed25519"),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("public_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("workspace_id", "alias", name="uq_wandb_credential_workspace_alias"),
    )
    op.create_index("ix_wandb_ingest_credentials_workspace_id", "wandb_ingest_credentials", ["workspace_id"])
    op.create_index("ix_wandb_ingest_credentials_project_id", "wandb_ingest_credentials", ["project_id"])
    op.create_index("ix_wandb_ingest_credentials_key_id", "wandb_ingest_credentials", ["key_id"])
    op.create_index("ix_wandb_ingest_credentials_state", "wandb_ingest_credentials", ["state"])

    op.create_table(
        "training_launches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_version_id", sa.String(length=36), nullable=False),
        sa.Column("wandb_credential_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="preparing"),
        sa.Column("client_request_id", sa.String(length=128), nullable=False),
        sa.Column("manifest_version", sa.String(length=64), nullable=False, server_default="modelfiche.training-launch.v1"),
        sa.Column("redacted_manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=True),
        sa.Column("dataset_export_job_id", sa.String(length=36), nullable=True),
        sa.Column("dataset_export_asset_id", sa.String(length=36), nullable=True),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("source_fingerprint", sa.String(length=96), nullable=True),
        sa.Column("run_prefix", sa.String(length=1024), nullable=False),
        sa.Column("backup_policy", sa.JSON(), nullable=False),
        sa.Column("checkpoint_policy", sa.JSON(), nullable=False),
        sa.Column("supported_endpoint_ids", sa.JSON(), nullable=False),
        sa.Column("credential_bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credential_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backup_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("checkpoint_handoff_status", sa.String(length=32), nullable=False, server_default="awaiting_manifest"),
        sa.Column("next_reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconcile_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reconcile_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dataset_version_id"], ["dataset_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wandb_credential_id"], ["wandb_ingest_credentials.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dataset_export_job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dataset_export_asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_id"], ["import_sources.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("run_id", name="uq_training_launch_run"),
        sa.UniqueConstraint("workspace_id", "client_request_id", name="uq_training_launch_client_request"),
    )
    for column in (
        "workspace_id",
        "project_id",
        "run_id",
        "dataset_version_id",
        "wandb_credential_id",
        "state",
        "manifest_digest",
        "dataset_export_job_id",
        "dataset_export_asset_id",
        "source_id",
        "backup_status",
        "checkpoint_handoff_status",
        "next_reconcile_at",
    ):
        op.create_index(f"ix_training_launches_{column}", "training_launches", [column])


def downgrade() -> None:
    op.drop_table("training_launches")
    op.drop_table("wandb_ingest_credentials")
    with op.batch_alter_table("training_runs") as batch:
        for name in (
            "exit_code",
            "last_event_at",
            "finished_at",
            "started_at",
            "current_step",
            "wandb_protocol_revision",
            "wandb_sdk_version",
            "wandb_display_name",
            "wandb_project",
            "wandb_run_id",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("dataset_versions") as batch:
        batch.drop_column("content_digest")
        batch.drop_column("lineage_kind")
        batch.drop_column("parent_version_id")
