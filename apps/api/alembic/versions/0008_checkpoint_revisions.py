"""Backfill operational checkpoint revision identity."""

from uuid import uuid4

from alembic import op

revision = "0008_checkpoint_revisions"
down_revision = "0007_asset_origin_timestamps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    checkpoints = bind.exec_driver_sql(
        "SELECT id, asset_id, created_at FROM checkpoints WHERE current_revision_id IS NULL ORDER BY created_at, id"
    ).fetchall()
    for checkpoint_id, asset_id, created_at in checkpoints:
        location = bind.exec_driver_sql(
            """
            SELECT id FROM asset_locations
            WHERE asset_id = ? AND verification_state IN ('available', 'verified')
            ORDER BY CASE WHEN provider = 'local' THEN 0 ELSE 1 END,
                     last_verified_at DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (asset_id,),
        ).fetchone()
        if location is None:
            continue
        asset = bind.exec_driver_sql("SELECT content_blob_id FROM assets WHERE id = ?", (asset_id,)).fetchone()
        revision_id = str(uuid4())
        bind.exec_driver_sql(
            """
            INSERT INTO checkpoint_revisions
              (id, checkpoint_id, ordinal, asset_id, content_blob_id, source_location_id, supersedes_id, created_at)
            VALUES (?, ?, 1, ?, ?, ?, NULL, ?)
            """,
            (revision_id, checkpoint_id, asset_id, asset[0] if asset else None, location[0], created_at),
        )
        bind.exec_driver_sql("UPDATE checkpoints SET current_revision_id = ? WHERE id = ?", (revision_id, checkpoint_id))
    bind.exec_driver_sql(
        """
        UPDATE model_versions
        SET checkpoint_revision_id = (
          SELECT checkpoints.current_revision_id FROM checkpoints
          WHERE checkpoints.id = model_versions.checkpoint_id
        )
        WHERE checkpoint_revision_id IS NULL
        """
    )


def downgrade() -> None:
    pass
