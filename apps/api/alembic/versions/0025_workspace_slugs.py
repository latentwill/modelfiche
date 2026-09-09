"""Add readable workspace slugs.

Revision ID: 0025_workspace_slugs
Revises: 0024_embedding_artifacts
"""

from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa

revision = "0025_workspace_slugs"
down_revision = "0024_embedding_artifacts"
branch_labels = None
depends_on = None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:80] or "workspace"


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("slug", sa.String(length=80), nullable=True))

    connection = op.get_bind()
    workspaces = sa.table(
        "workspaces",
        sa.column("id", sa.String(length=36)),
        sa.column("name", sa.String(length=200)),
        sa.column("slug", sa.String(length=80)),
        sa.column("created_at", sa.DateTime()),
    )
    rows = connection.execute(
        sa.select(workspaces.c.id, workspaces.c.name).order_by(
            workspaces.c.created_at, workspaces.c.id
        )
    ).all()
    used: set[str] = set()
    for workspace_id, name in rows:
        base = _slug(str(name))
        candidate = base
        suffix = 2
        while candidate in used:
            ending = f"-{suffix}"
            candidate = f"{base[: 80 - len(ending)]}{ending}"
            suffix += 1
        connection.execute(
            workspaces.update()
            .where(workspaces.c.id == workspace_id)
            .values(slug=candidate)
        )
        used.add(candidate)

    with op.batch_alter_table("workspaces") as batch:
        batch.alter_column("slug", existing_type=sa.String(length=80), nullable=False)
        batch.create_index("ix_workspaces_slug", ["slug"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_index("ix_workspaces_slug")
        batch.drop_column("slug")
