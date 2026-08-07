"""Make the W&B signing key global.

Revision ID: 0026_global_wandb_signing_key
Revises: 0025_workspace_slugs
"""

from __future__ import annotations

import hashlib
import json

from alembic import op
import sqlalchemy as sa

revision = "0026_global_wandb_signing_key"
down_revision = "0025_workspace_slugs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    credentials = sa.table(
        "wandb_ingest_credentials",
        sa.column("id", sa.String(length=36)),
        sa.column("alias", sa.String(length=120)),
        sa.column("key_id", sa.String(length=64)),
        sa.column("state", sa.String(length=32)),
        sa.column("last_used_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    launches = sa.table(
        "training_launches",
        sa.column("id", sa.String(length=36)),
        sa.column("wandb_credential_id", sa.String(length=36)),
        sa.column("redacted_manifest", sa.JSON()),
        sa.column("manifest_digest", sa.String(length=64)),
    )
    global_credential = connection.execute(
        sa.select(credentials.c.id, credentials.c.key_id)
        .order_by(
            sa.case((credentials.c.state == "active", 0), else_=1),
            sa.func.coalesce(
                credentials.c.last_used_at,
                credentials.c.updated_at,
                credentials.c.created_at,
            ).desc(),
            credentials.c.id,
        )
        .limit(1)
    ).first()
    if global_credential is not None:
        global_credential_id, global_key_id = global_credential
        connection.execute(
            launches.update().values(wandb_credential_id=global_credential_id)
        )
        connection.execute(
            credentials.delete().where(credentials.c.id != global_credential_id)
        )
        connection.execute(
            credentials.update()
            .where(credentials.c.id == global_credential_id)
            .values(alias="modelfiche")
        )
        for launch_id, raw_manifest in connection.execute(
            sa.select(launches.c.id, launches.c.redacted_manifest)
        ):
            manifest = (
                json.loads(raw_manifest)
                if isinstance(raw_manifest, str)
                else dict(raw_manifest or {})
            )
            telemetry = manifest.get("telemetry")
            if not isinstance(telemetry, dict):
                continue
            telemetry["credential_alias"] = "modelfiche"
            token = telemetry.get("token")
            if isinstance(token, dict):
                token["kid"] = global_key_id
                claims = token.get("claims")
                if isinstance(claims, dict):
                    claims["credential_id"] = global_credential_id
                    claims["kid"] = global_key_id
            digest = hashlib.sha256(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                launches.update()
                .where(launches.c.id == launch_id)
                .values(redacted_manifest=manifest, manifest_digest=digest)
            )

    with op.batch_alter_table("wandb_ingest_credentials") as batch:
        batch.drop_index("ix_wandb_ingest_credentials_workspace_id")
        batch.drop_index("ix_wandb_ingest_credentials_project_id")
        batch.drop_constraint("uq_wandb_credential_workspace_alias", type_="unique")
        batch.drop_column("workspace_id")
        batch.drop_column("project_id")
        batch.create_unique_constraint("uq_wandb_credential_alias", ["alias"])


def downgrade() -> None:
    with op.batch_alter_table("wandb_ingest_credentials") as batch:
        batch.drop_constraint("uq_wandb_credential_alias", type_="unique")
        batch.add_column(sa.Column("workspace_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))

    connection = op.get_bind()
    workspace_id = connection.scalar(sa.text("SELECT id FROM workspaces ORDER BY created_at, id LIMIT 1"))
    if workspace_id is not None:
        connection.execute(
            sa.text("UPDATE wandb_ingest_credentials SET workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )

    with op.batch_alter_table("wandb_ingest_credentials") as batch:
        batch.alter_column(
            "workspace_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch.create_foreign_key(
            "fk_wandb_ingest_credentials_workspace_id",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_wandb_ingest_credentials_project_id",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            "uq_wandb_credential_workspace_alias", ["workspace_id", "alias"]
        )
        batch.create_index(
            "ix_wandb_ingest_credentials_workspace_id", ["workspace_id"]
        )
        batch.create_index("ix_wandb_ingest_credentials_project_id", ["project_id"])
