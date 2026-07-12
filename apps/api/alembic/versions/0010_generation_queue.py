"""durable shared generation queue

Revision ID: 0010_generation_queue
Revises: 0009_inline_generation_prompts
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_generation_queue"
down_revision = "0009_inline_generation_prompts"
branch_labels = None
depends_on = None

def upgrade():
    if "generation_queue_items" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table("generation_queue_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_request_id", sa.String(36), nullable=False),
        sa.Column("workflow", sa.String(32), nullable=False), sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("queue_owner", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_id", sa.String(36), sa.ForeignKey("models.id", ondelete="SET NULL")),
        sa.Column("model_version_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("compiled_request_id", sa.String(36), nullable=False),
        sa.Column("admission_id", sa.String(36), sa.ForeignKey("fal_admissions.id", ondelete="SET NULL")),
        sa.Column("state", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("progress", sa.JSON(), nullable=False, server_default="{}"), sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text()), sa.Column("request_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("provider_job_id", sa.String(500)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.UniqueConstraint("workspace_id", "client_request_id", name="uq_generation_queue_client_request"))
    for column in ("workspace_id", "workflow", "provider", "queue_owner", "project_id", "model_id", "compiled_request_id", "admission_id", "state", "provider_job_id", "job_id"):
        op.create_index(f"ix_generation_queue_items_{column}", "generation_queue_items", [column])

def downgrade():
    op.drop_table("generation_queue_items")
