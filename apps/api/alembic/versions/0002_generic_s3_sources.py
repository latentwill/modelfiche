"""Frozen generic S3 source schema."""

from alembic import op
import sqlalchemy as sa


revision = "0002_generic_s3_sources"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("import_sources") as batch:
        batch.add_column(sa.Column("region", sa.String(length=100), nullable=True))
        batch.add_column(
            sa.Column("addressing_style", sa.String(length=16), nullable=False, server_default="auto")
        )
        batch.add_column(
            sa.Column("credential_env_prefix", sa.String(length=40), nullable=False, server_default="S3")
        )


def downgrade() -> None:
    raise NotImplementedError("the frozen compatibility baseline is not downgradable")
