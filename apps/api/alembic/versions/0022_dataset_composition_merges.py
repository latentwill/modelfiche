"""Represent dataset composition and checkpoint merge operations.

Revision ID: 0022_dataset_composition_merges
Revises: 0021_grid_eval_redesign
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0022_dataset_composition_merges"
down_revision = "0021_grid_eval_redesign"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
    ]


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value


def _version_digest(bind, version_id: str) -> str:
    version = bind.execute(sa.text("""
        SELECT name, source_uri, caption_format, trigger_words
        FROM dataset_versions WHERE id = :version_id
    """), {"version_id": version_id}).one()
    items = bind.execute(sa.text("""
        SELECT id, asset_id, caption, caption_format, included, tags, position
        FROM dataset_items WHERE dataset_version_id = :version_id
        ORDER BY position, asset_id, id
    """), {"version_id": version_id}).all()
    subsets = bind.execute(sa.text("""
        SELECT id, key, name, description, role, parent_subset_id, color_token, position
        FROM dataset_version_subsets WHERE dataset_version_id = :version_id
        ORDER BY position, key, id
    """), {"version_id": version_id}).all()
    subset_keys = {row.id: row.key for row in subsets}
    item_assets = {row.id: row.asset_id for row in items}
    memberships = bind.execute(sa.text("""
        SELECT m.subset_id, m.dataset_item_id, m.membership_role, m.position
        FROM dataset_subset_items m
        JOIN dataset_version_subsets s ON s.id = m.subset_id
        WHERE s.dataset_version_id = :version_id
    """), {"version_id": version_id}).all()
    memberships = sorted(memberships, key=lambda row: (
        subset_keys.get(row.subset_id, row.subset_id),
        row.position,
        item_assets.get(row.dataset_item_id, row.dataset_item_id),
    ))
    payload = {
        "version": {
            "name": version.name,
            "source_uri": version.source_uri,
            "caption_format": version.caption_format,
            "trigger_words": list(_json_value(version.trigger_words) or []),
        },
        "items": [{
            "asset_id": row.asset_id,
            "caption": row.caption,
            "caption_format": row.caption_format,
            "included": bool(row.included),
            "tags": list(_json_value(row.tags) or []),
            "position": row.position,
        } for row in items],
        "subsets": [{
            "key": row.key,
            "name": row.name,
            "description": row.description,
            "role": row.role,
            "parent_subset": subset_keys.get(row.parent_subset_id) if row.parent_subset_id else None,
            "color_token": row.color_token,
            "position": row.position,
        } for row in subsets],
        "memberships": [{
            "subset": subset_keys.get(row.subset_id, row.subset_id),
            "asset_id": item_assets.get(row.dataset_item_id, row.dataset_item_id),
            "membership_role": row.membership_role,
            "position": row.position,
        } for row in memberships],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    run_columns = {column["name"] for column in inspector.get_columns("training_runs")}
    if "run_kind" not in run_columns:
        op.add_column("training_runs", sa.Column("run_kind", sa.String(length=32), nullable=False, server_default="training"))
        op.create_index("ix_training_runs_run_kind", "training_runs", ["run_kind"])

    if "dataset_version_subsets" not in tables:
        op.create_table(
            "dataset_version_subsets",
            sa.Column("dataset_version_id", sa.String(length=36), nullable=False),
            sa.Column("key", sa.String(length=120), nullable=False),
            sa.Column("name", sa.String(length=240), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("role", sa.String(length=32), nullable=False, server_default="custom"),
            sa.Column("parent_subset_id", sa.String(length=36), nullable=True),
            sa.Column("color_token", sa.String(length=32), nullable=False, server_default="blue"),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["dataset_version_id"], ["dataset_versions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["parent_subset_id"], ["dataset_version_subsets.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("dataset_version_id", "key", name="uq_dataset_version_subset_key"),
        )
        op.create_index("ix_dataset_version_subsets_dataset_version_id", "dataset_version_subsets", ["dataset_version_id"])
        op.create_index("ix_dataset_version_subsets_parent_subset_id", "dataset_version_subsets", ["parent_subset_id"])
        op.create_index("ix_dataset_version_subsets_role", "dataset_version_subsets", ["role"])

    if "dataset_subset_items" not in tables:
        op.create_table(
            "dataset_subset_items",
            sa.Column("subset_id", sa.String(length=36), nullable=False),
            sa.Column("dataset_item_id", sa.String(length=36), nullable=False),
            sa.Column("membership_role", sa.String(length=32), nullable=False, server_default="primary"),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["subset_id"], ["dataset_version_subsets.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["dataset_item_id"], ["dataset_items.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("subset_id", "dataset_item_id", name="uq_dataset_subset_item"),
        )
        op.create_index("ix_dataset_subset_items_subset_id", "dataset_subset_items", ["subset_id"])
        op.create_index("ix_dataset_subset_items_dataset_item_id", "dataset_subset_items", ["dataset_item_id"])
        op.create_index("ix_dataset_subset_items_membership_role", "dataset_subset_items", ["membership_role"])

    if "dataset_draft_subsets" not in tables:
        op.create_table(
            "dataset_draft_subsets",
            sa.Column("draft_id", sa.String(length=36), nullable=False),
            sa.Column("source_subset_id", sa.String(length=36), nullable=True),
            sa.Column("key", sa.String(length=120), nullable=False),
            sa.Column("name", sa.String(length=240), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("role", sa.String(length=32), nullable=False, server_default="custom"),
            sa.Column("parent_subset_id", sa.String(length=36), nullable=True),
            sa.Column("color_token", sa.String(length=32), nullable=False, server_default="blue"),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["draft_id"], ["dataset_drafts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_subset_id"], ["dataset_version_subsets.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["parent_subset_id"], ["dataset_draft_subsets.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("draft_id", "key", name="uq_dataset_draft_subset_key"),
        )
        op.create_index("ix_dataset_draft_subsets_draft_id", "dataset_draft_subsets", ["draft_id"])
        op.create_index("ix_dataset_draft_subsets_source_subset_id", "dataset_draft_subsets", ["source_subset_id"])
        op.create_index("ix_dataset_draft_subsets_parent_subset_id", "dataset_draft_subsets", ["parent_subset_id"])
        op.create_index("ix_dataset_draft_subsets_role", "dataset_draft_subsets", ["role"])

    if "dataset_draft_subset_items" not in tables:
        op.create_table(
            "dataset_draft_subset_items",
            sa.Column("subset_id", sa.String(length=36), nullable=False),
            sa.Column("draft_item_id", sa.String(length=36), nullable=False),
            sa.Column("membership_role", sa.String(length=32), nullable=False, server_default="primary"),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["subset_id"], ["dataset_draft_subsets.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["draft_item_id"], ["dataset_draft_items.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("subset_id", "draft_item_id", name="uq_dataset_draft_subset_item"),
        )
        op.create_index("ix_dataset_draft_subset_items_subset_id", "dataset_draft_subset_items", ["subset_id"])
        op.create_index("ix_dataset_draft_subset_items_draft_item_id", "dataset_draft_subset_items", ["draft_item_id"])
        op.create_index("ix_dataset_draft_subset_items_membership_role", "dataset_draft_subset_items", ["membership_role"])

    if "training_run_dataset_inputs" not in tables:
        op.create_table(
            "training_run_dataset_inputs",
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("dataset_version_id", sa.String(length=36), nullable=False),
            sa.Column("subset_id", sa.String(length=36), nullable=True),
            sa.Column("alias", sa.String(length=120), nullable=True),
            sa.Column("item_count_snapshot", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sampling_weight", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("repeat_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("dataset_content_digest", sa.String(length=64), nullable=True),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["dataset_version_id"], ["dataset_versions.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["subset_id"], ["dataset_version_subsets.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("run_id", "dataset_version_id", "subset_id", name="uq_training_run_dataset_input"),
        )
        op.create_index("ix_training_run_dataset_inputs_run_id", "training_run_dataset_inputs", ["run_id"])
        op.create_index("ix_training_run_dataset_inputs_dataset_version_id", "training_run_dataset_inputs", ["dataset_version_id"])
        op.create_index("ix_training_run_dataset_inputs_subset_id", "training_run_dataset_inputs", ["subset_id"])
        op.create_index("ix_training_run_dataset_inputs_dataset_content_digest", "training_run_dataset_inputs", ["dataset_content_digest"])

    if "merge_operations" not in tables:
        op.create_table(
            "merge_operations",
            sa.Column("workspace_id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("schema_version", sa.String(length=80), nullable=False, server_default="modelfiche.checkpoint-merge/v1"),
            sa.Column("operator", sa.String(length=64), nullable=False),
            sa.Column("base_model", sa.String(length=500), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="preparing"),
            sa.Column("recipe", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("recipe_digest", sa.String(length=64), nullable=False),
            sa.Column("compact_notation", sa.Text(), nullable=True),
            sa.Column("output_rank", sa.Integer(), nullable=False),
            sa.Column("dtype", sa.String(length=32), nullable=False),
            sa.Column("output_checkpoint_revision_id", sa.String(length=36), nullable=True),
            sa.Column("error_code", sa.String(length=80), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("failure_phase", sa.String(length=80), nullable=True),
            sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["run_id"], ["training_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["output_checkpoint_revision_id"], ["checkpoint_revisions.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("project_id", "recipe_digest", name="uq_merge_operation_recipe"),
            sa.UniqueConstraint("run_id", name="uq_merge_operation_run"),
        )
        for column in ("workspace_id", "project_id", "run_id", "operator", "status", "recipe_digest", "output_checkpoint_revision_id"):
            op.create_index(f"ix_merge_operations_{column}", "merge_operations", [column])

    if "merge_inputs" not in tables:
        op.create_table(
            "merge_inputs",
            sa.Column("merge_operation_id", sa.String(length=36), nullable=False),
            sa.Column("checkpoint_revision_id", sa.String(length=36), nullable=False),
            sa.Column("alias", sa.String(length=120), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False, server_default="custom"),
            sa.Column("source_rank", sa.Integer(), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["merge_operation_id"], ["merge_operations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["checkpoint_revision_id"], ["checkpoint_revisions.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("merge_operation_id", "alias", name="uq_merge_input_alias"),
            sa.UniqueConstraint("merge_operation_id", "position", name="uq_merge_input_position"),
        )
        op.create_index("ix_merge_inputs_merge_operation_id", "merge_inputs", ["merge_operation_id"])
        op.create_index("ix_merge_inputs_checkpoint_revision_id", "merge_inputs", ["checkpoint_revision_id"])

    if "merge_input_weights" not in tables:
        op.create_table(
            "merge_input_weights",
            sa.Column("merge_input_id", sa.String(length=36), nullable=False),
            sa.Column("scope", sa.String(length=64), nullable=False),
            sa.Column("module_pattern", sa.String(length=500), nullable=True),
            sa.Column("weight", sa.Float(), nullable=False),
            *_timestamps(),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["merge_input_id"], ["merge_inputs.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("merge_input_id", "scope", "module_pattern", name="uq_merge_input_weight_scope"),
        )
        op.create_index("ix_merge_input_weights_merge_input_id", "merge_input_weights", ["merge_input_id"])
        op.create_index("ix_merge_input_weights_scope", "merge_input_weights", ["scope"])

    now = datetime.now(timezone.utc)
    versions = bind.execute(sa.text("SELECT id FROM dataset_versions")).all()
    for (version_id,) in versions:
        existing = bind.execute(sa.text("SELECT id FROM dataset_version_subsets WHERE dataset_version_id = :version_id LIMIT 1"), {"version_id": version_id}).first()
        if existing:
            continue
        subset_id = str(uuid.uuid4())
        bind.execute(sa.text("""
            INSERT INTO dataset_version_subsets
                (dataset_version_id, key, name, description, role, parent_subset_id, color_token, position, created_at, updated_at, id)
            VALUES (:version_id, 'default', 'Default', NULL, 'custom', NULL, 'blue', 0, :now, :now, :id)
        """), {"version_id": version_id, "now": now, "id": subset_id})
        items = bind.execute(sa.text("SELECT id, position FROM dataset_items WHERE dataset_version_id = :version_id"), {"version_id": version_id}).all()
        for item_id, position in items:
            bind.execute(sa.text("""
                INSERT INTO dataset_subset_items
                    (subset_id, dataset_item_id, membership_role, position, created_at, updated_at, id)
                VALUES (:subset_id, :item_id, 'primary', :position, :now, :now, :id)
            """), {"subset_id": subset_id, "item_id": item_id, "position": position, "now": now, "id": str(uuid.uuid4())})

    dawn_jian_groups = (
        ("core-gestures", "Core gestures", "core gestures", "blue"),
        ("atmospheric-diffusion-layers", "Atmospheric diffusion layers", "atmospheric diffusion layers", "cyan"),
        ("digital-horizon-paintings", "Digital horizon paintings", "digital horizon paintings", "teal"),
        ("texture-surface", "Texture surface", "texture surface", "green"),
    )
    dawn_jian_versions = bind.execute(sa.text("""
        SELECT v.id
        FROM dataset_versions v
        JOIN datasets d ON d.id = v.dataset_id
        WHERE lower(d.name) = 'dawnjian'
    """)).all()
    for (version_id,) in dawn_jian_versions:
        item_paths = bind.execute(sa.text("""
            SELECT DISTINCT i.id, i.position, l.uri
            FROM dataset_items i
            JOIN asset_locations l ON l.asset_id = i.asset_id
            WHERE i.dataset_version_id = :version_id AND l.provider = 's3'
        """), {"version_id": version_id}).all()
        assignments_by_item: dict[str, tuple[str, int, str]] = {}
        for item_id, position, uri in item_paths:
            normalized_uri = str(uri).lower()
            group_key = next((key for key, _name, folder, _color in dawn_jian_groups if f"/dawnjian/{folder}/" in normalized_uri), None)
            if group_key:
                assignments_by_item[item_id] = (item_id, position, group_key)
        assignments = list(assignments_by_item.values())
        item_count = bind.execute(sa.text("SELECT count(*) FROM dataset_items WHERE dataset_version_id = :version_id"), {"version_id": version_id}).scalar_one()
        if len(assignments) != item_count or {assignment[2] for assignment in assignments} != {group[0] for group in dawn_jian_groups}:
            continue
        old_subsets = bind.execute(sa.text("SELECT id FROM dataset_version_subsets WHERE dataset_version_id = :version_id"), {"version_id": version_id}).all()
        for (old_subset_id,) in old_subsets:
            bind.execute(sa.text("DELETE FROM dataset_subset_items WHERE subset_id = :subset_id"), {"subset_id": old_subset_id})
        bind.execute(sa.text("DELETE FROM dataset_version_subsets WHERE dataset_version_id = :version_id"), {"version_id": version_id})
        subset_ids: dict[str, str] = {}
        for position, (key, name, _folder, color) in enumerate(dawn_jian_groups):
            subset_id = str(uuid.uuid4())
            subset_ids[key] = subset_id
            bind.execute(sa.text("""
                INSERT INTO dataset_version_subsets
                    (dataset_version_id, key, name, description, role, parent_subset_id, color_token, position, created_at, updated_at, id)
                VALUES (:version_id, :key, :name, 'Inferred from the imported Dawn Jian top-level folder.', 'style', NULL, :color, :position, :now, :now, :id)
            """), {"version_id": version_id, "key": key, "name": name, "color": color, "position": position, "now": now, "id": subset_id})
        for item_id, position, key in assignments:
            bind.execute(sa.text("""
                INSERT INTO dataset_subset_items
                    (subset_id, dataset_item_id, membership_role, position, created_at, updated_at, id)
                VALUES (:subset_id, :item_id, 'primary', :position, :now, :now, :id)
            """), {"subset_id": subset_ids[key], "item_id": item_id, "position": position, "now": now, "id": str(uuid.uuid4())})
        bind.execute(sa.text("UPDATE dataset_versions SET content_digest = :digest WHERE id = :version_id"), {
            "digest": _version_digest(bind, version_id),
            "version_id": version_id,
        })

    bind.execute(sa.text("UPDATE training_runs SET run_kind = 'checkpoint_merge' WHERE lower(coalesce(trainer, '')) = 'checkpoint-merge'"))
    runs = bind.execute(sa.text("""
        SELECT r.id, r.dataset_version_id, v.content_digest,
               (SELECT count(*) FROM dataset_items i WHERE i.dataset_version_id = r.dataset_version_id AND i.included = 1)
        FROM training_runs r
        JOIN dataset_versions v ON v.id = r.dataset_version_id
        WHERE r.dataset_version_id IS NOT NULL
    """)).all()
    for run_id, version_id, digest, item_count in runs:
        existing = bind.execute(sa.text("SELECT id FROM training_run_dataset_inputs WHERE run_id = :run_id LIMIT 1"), {"run_id": run_id}).first()
        if existing:
            continue
        bind.execute(sa.text("""
            INSERT INTO training_run_dataset_inputs
                (run_id, dataset_version_id, subset_id, alias, item_count_snapshot, sampling_weight, repeat_count, position, dataset_content_digest, created_at, updated_at, id)
            VALUES (:run_id, :version_id, NULL, NULL, :item_count, 1.0, 1, 0, :digest, :now, :now, :id)
        """), {"run_id": run_id, "version_id": version_id, "item_count": item_count, "digest": digest, "now": now, "id": str(uuid.uuid4())})


def downgrade() -> None:
    for table in (
        "merge_input_weights",
        "merge_inputs",
        "merge_operations",
        "training_run_dataset_inputs",
        "dataset_draft_subset_items",
        "dataset_draft_subsets",
        "dataset_subset_items",
        "dataset_version_subsets",
    ):
        op.drop_table(table)
    with op.batch_alter_table("training_runs") as batch:
        batch.drop_index("ix_training_runs_run_kind")
        batch.drop_column("run_kind")
