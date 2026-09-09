"""ordered children for shared generation queue

Revision ID: 0011_generation_queue_children
Revises: 0010_generation_queue
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_generation_queue_children"
down_revision = "0010_generation_queue"
branch_labels = None
depends_on = None

def upgrade():
    if "generation_queue_children" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table("generation_queue_children",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("queue_item_id", sa.String(36), sa.ForeignKey("generation_queue_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.String(36), sa.ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("compiled_request_id", sa.String(36), nullable=False),
        sa.Column("admission_id", sa.String(36), sa.ForeignKey("fal_admissions.id", ondelete="SET NULL")),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("provider_job_id", sa.String(500)),
        sa.Column("state", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("progress", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("queue_item_id", "ordinal", name="uq_generation_queue_child_ordinal"),
        sa.UniqueConstraint("queue_item_id", "compiled_request_id", name="uq_generation_queue_child_request"))
    for column in ("queue_item_id", "model_version_id", "compiled_request_id", "admission_id", "job_id", "provider_job_id", "state"):
        op.create_index(f"ix_generation_queue_children_{column}", "generation_queue_children", [column])

def downgrade():
    op.drop_table("generation_queue_children")
