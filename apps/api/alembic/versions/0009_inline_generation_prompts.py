"""Allow generated workflows to persist ordered prompts without PromptSet rows."""

from alembic import op
import sqlalchemy as sa

revision = "0009_inline_generation_prompts"
down_revision = "0008_checkpoint_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("eval_definitions") as batch:
        batch.alter_column("prompt_set_id", existing_type=sa.String(length=36), nullable=True)
        batch.add_column(sa.Column("inline_prompts", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("eval_definitions") as batch:
        batch.drop_column("inline_prompts")
        batch.alter_column("prompt_set_id", existing_type=sa.String(length=36), nullable=False)
