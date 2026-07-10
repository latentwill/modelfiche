"""Store generic S3 connection behavior without credentials."""

from alembic import op
import sqlalchemy as sa

revision = "0002_generic_s3_sources"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("import_sources")}
    with op.batch_alter_table("import_sources") as batch:
        if "region" not in existing:
            batch.add_column(sa.Column("region", sa.String(length=100), nullable=True))
        if "addressing_style" not in existing:
            batch.add_column(sa.Column("addressing_style", sa.String(length=16), nullable=False, server_default="auto"))
        if "credential_env_prefix" not in existing:
            batch.add_column(sa.Column("credential_env_prefix", sa.String(length=40), nullable=False, server_default="S3"))


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("import_sources")}
    with op.batch_alter_table("import_sources") as batch:
        if "credential_env_prefix" in existing:
            batch.drop_column("credential_env_prefix")
        if "addressing_style" in existing:
            batch.drop_column("addressing_style")
        if "region" in existing:
            batch.drop_column("region")
