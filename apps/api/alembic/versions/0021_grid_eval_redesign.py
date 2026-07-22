"""Persist immutable project-owned experiment plans and target-aware grid cells.

Revision ID: 0021_grid_eval_redesign
Revises: 0020_worker_heartbeats
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0021_grid_eval_redesign"
down_revision = "0020_worker_heartbeats"
branch_labels = None
depends_on = None


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _add_column(inspector, table: str, column: sa.Column) -> None:
    if column.name not in {item["name"] for item in inspector.get_columns(table)}:
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "experiment_plans" not in tables:
        op.create_table(
            "experiment_plans",
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("contract_version", sa.String(length=32), nullable=False, server_default="2026-07-22.v1"),
            sa.Column("plan_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("digest", sa.String(length=80), nullable=False),
            sa.Column("snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("idempotency_key", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("project_id", "digest", name="uq_experiment_plan_project_digest"),
            sa.UniqueConstraint("project_id", "idempotency_key", name="uq_experiment_plan_project_idempotency"),
        )
        op.create_index("ix_experiment_plans_digest", "experiment_plans", ["digest"])
        op.create_index("ix_experiment_plans_project_id", "experiment_plans", ["project_id"])
    inspector = sa.inspect(bind)
    for table, columns in {
        "eval_definitions": [
            sa.Column("plan_id", sa.String(length=36), nullable=True),
            sa.Column("plan_version", sa.Integer(), nullable=True),
            sa.Column("plan_digest", sa.String(length=80), nullable=True),
        ],
        "eval_runs": [
            sa.Column("plan_id", sa.String(length=36), nullable=True),
            sa.Column("plan_version", sa.Integer(), nullable=True),
            sa.Column("plan_digest", sa.String(length=80), nullable=True),
        ],
        "grid_definitions": [
            sa.Column("z_axis", sa.JSON(), nullable=True),
            sa.Column("plan_id", sa.String(length=36), nullable=True),
            sa.Column("plan_version", sa.Integer(), nullable=True),
            sa.Column("plan_digest", sa.String(length=80), nullable=True),
            sa.Column("plan_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
            sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_cost", sa.String(length=32), nullable=False, server_default="0.000000"),
            sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        ],
        "grid_cells": [
            sa.Column("ordinal", sa.Integer(), nullable=True),
            sa.Column("z_index", sa.Integer(), nullable=False, server_default="-1"),
            sa.Column("coordinate", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("case_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("target_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("endpoint_id", sa.String(length=240), nullable=False, server_default=""),
            sa.Column("endpoint_adapter", sa.String(length=64), nullable=False, server_default="fal"),
            sa.Column("schema_digest", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("effective_params", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("output_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("request_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("estimated_cost", sa.String(length=32), nullable=False, server_default="0.000000"),
            sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        ],
    }.items():
        if table in tables:
            for column in columns:
                _add_column(inspector, table, column)
            inspector = sa.inspect(bind)
    # Add the ownership links and lookup indexes for the ORM ForeignKey columns.
    for table, name, local, remote_table in (
        ("eval_definitions", "fk_eval_definitions_plan_id", "plan_id", "experiment_plans"),
        ("eval_runs", "fk_eval_runs_plan_id", "plan_id", "experiment_plans"),
        ("grid_definitions", "fk_grid_definitions_plan_id", "plan_id", "experiment_plans"),
    ):
        if table in tables:
            with op.batch_alter_table(table) as batch:
                batch.create_foreign_key(name, remote_table, [local], ["id"], ondelete="SET NULL" if table != "grid_definitions" else "RESTRICT")
            index_name = f"ix_{table}_{local}"
            if index_name not in {item["name"] for item in sa.inspect(bind).get_indexes(table)}:
                op.create_index(index_name, table, [local])
    if "eval_assessments" not in tables:
        op.create_table(
            "eval_assessments",
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=240), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("plan_id", sa.String(length=36), nullable=False),
            sa.Column("plan_version", sa.Integer(), nullable=False),
            sa.Column("plan_digest", sa.String(length=80), nullable=False),
            sa.Column("assessment", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="ready_for_review"),
            sa.Column("idempotency_key", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["run_id"], ["eval_runs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["plan_id"], ["experiment_plans.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", "idempotency_key", name="uq_eval_assessment_project_idempotency"),
        )
        op.create_index("ix_eval_assessments_project_id", "eval_assessments", ["project_id"])
        op.create_index("ix_eval_assessments_run_id", "eval_assessments", ["run_id"])
        op.create_index("ix_eval_assessments_plan_id", "eval_assessments", ["plan_id"])
        op.create_index("ix_eval_assessments_plan_digest", "eval_assessments", ["plan_digest"])
        op.create_index("ix_eval_assessments_status", "eval_assessments", ["status"])
    # Historical PromptSet-backed definitions are copied to immutable inline cases.
    plan_table = sa.table("experiment_plans", sa.column("id", sa.String), sa.column("project_id", sa.String), sa.column("contract_version", sa.String), sa.column("plan_version", sa.Integer), sa.column("digest", sa.String), sa.column("snapshot", sa.JSON), sa.column("idempotency_key", sa.String), sa.column("created_at", sa.DateTime), sa.column("updated_at", sa.DateTime))
    now = datetime.now(timezone.utc)
    if "eval_definitions" in tables and "prompt_sets" in tables:
        definitions = sa.table("eval_definitions", sa.column("id", sa.String), sa.column("project_id", sa.String), sa.column("endpoint", sa.String), sa.column("model_version_id", sa.String), sa.column("prompt_set_id", sa.String), sa.column("inline_prompts", sa.JSON), sa.column("parameters", sa.JSON), sa.column("plan_id", sa.String), sa.column("plan_version", sa.Integer), sa.column("plan_digest", sa.String))
        prompts = sa.table("prompts", sa.column("id", sa.String), sa.column("prompt_set_id", sa.String), sa.column("text", sa.Text), sa.column("position", sa.Integer), sa.column("metadata", sa.JSON))
        rows = bind.execute(sa.select(definitions).where(definitions.c.plan_id.is_(None))).mappings().all()
        for row in rows:
            inline = list(row["inline_prompts"] or [])
            if not inline and row["prompt_set_id"]:
                inline = [{"id": str(item["id"]), "text": str(item["text"]), "position": int(item["position"] or 0), "metadata": dict(item["metadata"] or {})} for item in bind.execute(sa.select(prompts).where(prompts.c.prompt_set_id == row["prompt_set_id"]).order_by(prompts.c.position, prompts.c.id)).mappings()]
            cases = [{"case_id": str(item.get("id")), "ordinal": int(item.get("position", index)), "input": {"prompt": item.get("text", ""), "variables": dict(item.get("metadata") or {})}, "input_digest": _digest({"prompt": item.get("text", ""), "variables": dict(item.get("metadata") or {})})} for index, item in enumerate(inline)]
            # No source cases is historical and must remain explicitly non-runnable.
            historical_status = "non_runnable" if not cases else "ready"
            target = {"target_id": "legacy-target", "ordinal": 0, "provider": "fal", "endpoint_id": row["endpoint"], "model_version_id": row["model_version_id"], "checkpoint_revision_id": None, "schema_digest": "legacy", "schema": {"request_fields": [], "defaults": {}, "fields": {}, "grid": {}}, "fixed_target": {"provider": "fal", "endpoint_id": row["endpoint"], "model_version_id": row["model_version_id"], "checkpoint_revision_id": None}, "shared_overrides": {}, "target_overrides": {}}
            snapshot = {"contract_version": "2026-07-22.v1", "project_id": row["project_id"], "plan_version": 1, "axes": {"x": {"name": "legacy", "values": [0]}, "y": {"name": "legacy_y", "values": [0]}, "z": None}, "cases": cases, "targets": [target], "fixed_case_id": cases[0]["case_id"] if cases else None, "fixed_target_id": "legacy-target", "shared_params": dict(row["parameters"] or {}), "endpoint_defaults": {}, "status": historical_status}
            plan_digest = _digest(snapshot)
            existing_plan = bind.execute(sa.text("SELECT id FROM experiment_plans WHERE project_id=:project AND digest=:digest LIMIT 1"), {"project": row["project_id"], "digest": plan_digest}).scalar_one_or_none()
            plan_id = str(existing_plan or uuid.uuid4())
            if existing_plan is None:
                bind.execute(plan_table.insert().values(id=plan_id, project_id=row["project_id"], contract_version="2026-07-22.v1", plan_version=1, digest=plan_digest, snapshot={**snapshot, "plan_id": plan_id, "digest": plan_digest}, idempotency_key=None, created_at=now, updated_at=now))
            bind.execute(definitions.update().where(definitions.c.id == row["id"]).values(inline_prompts=inline, plan_id=plan_id, plan_version=1, plan_digest=plan_digest))
    if "grid_definitions" in tables and "eval_definitions" in tables:
        grids = sa.table("grid_definitions", sa.column("id", sa.String), sa.column("eval_definition_id", sa.String), sa.column("x_axis", sa.JSON), sa.column("y_axis", sa.JSON), sa.column("z_axis", sa.JSON), sa.column("plan_id", sa.String), sa.column("plan_version", sa.Integer), sa.column("plan_digest", sa.String), sa.column("plan_snapshot", sa.JSON), sa.column("status", sa.String), sa.column("request_count", sa.Integer), sa.column("estimated_cost", sa.String))
        plans = sa.table("experiment_plans", sa.column("id", sa.String), sa.column("project_id", sa.String), sa.column("plan_version", sa.Integer), sa.column("digest", sa.String), sa.column("snapshot", sa.JSON))
        definitions = sa.table("eval_definitions", sa.column("id", sa.String), sa.column("project_id", sa.String), sa.column("plan_id", sa.String))
        for row in bind.execute(sa.select(grids).where(grids.c.plan_id.is_(None))).mappings():
            definition = bind.execute(sa.select(definitions).where(definitions.c.id == row["eval_definition_id"])).mappings().first()
            if not definition or not definition["plan_id"]:
                continue
            plan = bind.execute(sa.select(plans).where(plans.c.id == definition["plan_id"])).mappings().first()
            if not plan:
                continue
            snapshot = dict(plan["snapshot"] or {})
            snapshot["axes"] = {"x": dict(row["x_axis"] or {}), "y": dict(row["y_axis"] or {}), "z": row["z_axis"]}
            snapshot.pop("plan_id", None)
            snapshot.pop("digest", None)
            plan_digest = _digest(snapshot)
            existing_id = bind.execute(sa.text("SELECT id FROM experiment_plans WHERE project_id=:project AND digest=:digest LIMIT 1"), {"project": snapshot["project_id"], "digest": plan_digest}).scalar_one_or_none()
            plan_id = str(existing_id or uuid.uuid4())
            stored_snapshot = {**snapshot, "plan_id": plan_id, "digest": plan_digest}
            if existing_id is None:
                bind.execute(plan_table.insert().values(id=plan_id, project_id=snapshot["project_id"], contract_version=snapshot.get("contract_version", "2026-07-22.v1"), plan_version=int(snapshot.get("plan_version", 1)), digest=plan_digest, snapshot=stored_snapshot, idempotency_key=None, created_at=now, updated_at=now))
            bind.execute(grids.update().where(grids.c.id == row["id"]).values(plan_id=plan_id, plan_version=int(snapshot.get("plan_version", 1)), plan_digest=plan_digest, plan_snapshot=stored_snapshot, status="draft", request_count=0, estimated_cost="0.000000"))
    if "eval_runs" in tables and "eval_definitions" in tables:
        bind.execute(sa.text("UPDATE eval_runs SET plan_id=(SELECT plan_id FROM eval_definitions WHERE eval_definitions.id=eval_runs.definition_id), plan_version=(SELECT plan_version FROM eval_definitions WHERE eval_definitions.id=eval_runs.definition_id), plan_digest=(SELECT plan_digest FROM eval_definitions WHERE eval_definitions.id=eval_runs.definition_id) WHERE plan_id IS NULL"))
    if "grid_cells" in tables:
        # Preserve historical coordinate identity while making output linkage optional for pending cells.
        bind.execute(sa.text("UPDATE grid_cells SET z_index = COALESCE(z_index,-1), ordinal = COALESCE(ordinal, x_index + y_index * 100000), coordinate = CASE WHEN coordinate = '{}' THEN json_object('x', x_index, 'y', y_index, 'z', NULL) ELSE coordinate END WHERE ordinal IS NULL OR z_index IS NULL OR coordinate = '{}'"))
    for table, column in (("grid_definitions", "eval_definition_id"), ("grid_cells", "eval_output_id")):
        if table in tables and column in {item["name"] for item in sa.inspect(bind).get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.alter_column(column, nullable=True)
    if "grid_cells" in tables:
        with op.batch_alter_table("grid_cells") as batch:
            batch.alter_column("z_index", nullable=False, server_default="-1")
    if "generation_queue_children" in tables:
        with op.batch_alter_table("generation_queue_children") as batch:
            batch.alter_column("model_version_id", nullable=True)
    for table, name, cols in (
        ("grid_cells", "uq_grid_cell_ordinal", ["grid_definition_id", "ordinal"]),
        ("grid_cells", "uq_grid_cell_coordinate", ["grid_definition_id", "x_index", "y_index", "z_index"]),
        ("grid_definitions", "uq_grid_definition_project_idempotency", ["project_id", "idempotency_key"]),
    ):
        if table not in tables:
            continue
        indexes = {item["name"]: item for item in sa.inspect(bind).get_indexes(table)}
        existing = indexes.get(name)
        if existing is None:
            op.create_index(name, table, cols, unique=True)
        elif not existing.get("unique"):
            raise RuntimeError(f"{name} exists but is not unique")


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "eval_assessments" in tables:
        op.drop_index("ix_eval_assessments_status", table_name="eval_assessments")
        op.drop_index("ix_eval_assessments_plan_digest", table_name="eval_assessments")
        op.drop_index("ix_eval_assessments_plan_id", table_name="eval_assessments")
        op.drop_index("ix_eval_assessments_run_id", table_name="eval_assessments")
        op.drop_index("ix_eval_assessments_project_id", table_name="eval_assessments")
        op.drop_table("eval_assessments")
    if "generation_queue_children" in tables:
        null_count = bind.execute(sa.text("SELECT COUNT(*) FROM generation_queue_children WHERE model_version_id IS NULL")).scalar_one()
        if null_count:
            raise RuntimeError("cannot downgrade generation_queue_children with endpoint-only rows")
        with op.batch_alter_table("generation_queue_children") as batch:
            batch.alter_column("model_version_id", nullable=False)
    for table, name in (
        ("grid_definitions", "ix_grid_definitions_plan_id"),
        ("eval_runs", "ix_eval_runs_plan_id"),
        ("eval_definitions", "ix_eval_definitions_plan_id"),
        ("grid_cells", "uq_grid_cell_ordinal"),
        ("grid_cells", "uq_grid_cell_coordinate"),
        ("grid_definitions", "uq_grid_definition_project_idempotency"),
    ):
        if table in tables and name in {item["name"] for item in sa.inspect(bind).get_indexes(table)}:
            op.drop_index(name, table_name=table)
    for table, name in (
        ("grid_definitions", "fk_grid_definitions_plan_id"),
        ("eval_runs", "fk_eval_runs_plan_id"),
        ("eval_definitions", "fk_eval_definitions_plan_id"),
    ):
        if table in tables:
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(name, type_="foreignkey")
    for table, columns in (
        ("grid_cells", ["idempotency_key", "estimated_cost", "request_count", "output_snapshot", "status", "effective_params", "schema_digest", "endpoint_adapter", "endpoint_id", "target_snapshot", "case_snapshot", "coordinate", "z_index", "ordinal"]),
        ("grid_definitions", ["idempotency_key", "estimated_cost", "request_count", "status", "plan_snapshot", "plan_digest", "plan_version", "plan_id", "z_axis"]),
        ("eval_runs", ["plan_digest", "plan_version", "plan_id"]),
        ("eval_definitions", ["plan_digest", "plan_version", "plan_id"]),
    ):
        if table in tables:
            with op.batch_alter_table(table) as batch:
                for column in columns:
                    if column in {item["name"] for item in sa.inspect(bind).get_columns(table)}:
                        batch.drop_column(column)
    if "experiment_plans" in tables:
        op.drop_index("ix_experiment_plans_project_id", table_name="experiment_plans")
        op.drop_index("ix_experiment_plans_digest", table_name="experiment_plans")
        op.drop_table("experiment_plans")
