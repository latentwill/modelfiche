"""Backfill training-sample asset ownership.

Revision ID: 0012_backfill_sample_project_ownership
Revises: 0011_generation_queue_children
"""
from collections import defaultdict

from alembic import op
import sqlalchemy as sa


revision = "0012_backfill_sample_project_ownership"
down_revision = "0011_generation_queue_children"
branch_labels = None
depends_on = None


def upgrade():
    """Make each unambiguous training sample an asset of its run's project."""
    bind = op.get_bind()
    assets = sa.table(
        "assets",
        sa.column("id", sa.String()),
        sa.column("project_id", sa.String()),
        sa.column("metadata", sa.JSON()),
    )
    samples = sa.table("samples", sa.column("asset_id", sa.String()), sa.column("run_id", sa.String()))
    runs = sa.table("training_runs", sa.column("id", sa.String()), sa.column("project_id", sa.String()))

    projects_by_asset: dict[str, set[str]] = defaultdict(set)
    for asset_id, project_id in bind.execute(
        sa.select(samples.c.asset_id, runs.c.project_id).join(runs, runs.c.id == samples.c.run_id)
    ):
        projects_by_asset[asset_id].add(project_id)

    for asset_id, project_ids in projects_by_asset.items():
        asset = bind.execute(
            sa.select(assets.c.metadata).where(assets.c.id == asset_id)
        ).one()
        metadata = {**dict(asset._mapping["metadata"] or {}), "category": "sample"}
        values: dict[str, object] = {"metadata": metadata}
        if len(project_ids) == 1:
            values["project_id"] = project_ids.pop()
        bind.execute(assets.update().where(assets.c.id == asset_id).values(**values))


def downgrade():
    # The previous project/category values cannot be recovered after a data repair.
    pass
