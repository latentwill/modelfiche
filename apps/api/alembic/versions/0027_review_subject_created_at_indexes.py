"""Add indexes for review and comment subject timelines.

Revision ID: 0027_review_subject_created_at_indexes
Revises: 0026_global_wandb_signing_key
"""

from __future__ import annotations

from alembic import op

revision = "0027_review_subject_created_at_indexes"
down_revision = "0026_global_wandb_signing_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_reviews_subject_created_at",
        "reviews",
        ["subject_type", "subject_id", "created_at"],
    )
    op.create_index(
        "ix_comments_subject_created_at",
        "comments",
        ["subject_type", "subject_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_comments_subject_created_at", table_name="comments")
    op.drop_index("ix_reviews_subject_created_at", table_name="reviews")
