"""Reconcile the historical Modern Times full3x checkpoint merge.

Revision ID: 0023_modern_times_merge_reconciliation
Revises: 0022_dataset_composition_merges
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0023_modern_times_merge_reconciliation"
down_revision = "0022_dataset_composition_merges"
branch_labels = None
depends_on = None

RUN_ID = "35a64180-8830-4153-8c09-b5505c98d140"
OUTPUT_REVISION_ID = "263bf54d-5092-4301-ab61-4f8b7e957877"
SOURCES = (
    ("full", "998b080b-023f-4c4e-94fa-b3422f5eb017", "01a84ecf258025cd936f1e405374ad7a6e836df98c5ebadcde03444aaa817ae2", 2400, 32, "general", 0.35, 0.20, "native"),
    ("render", "43b893b4-c1bf-46c1-8401-d0df0aee234a", "849705de6ee2f73f5d50920ec6043de89a79a555fb73e609dc5783ca6820fd07", 3000, 64, "specialist", 0.40, 0.50, "ai_toolkit"),
    ("flat", "39f7b43f-67dd-4944-aa1d-ec2c962077c3", "f081b7dc6e18bf7d4ddcc0a0c6858625a1af64a9346757ece427759027e3bbbd", 3250, 64, "specialist", 0.25, 0.30, "ai_toolkit"),
)


def _recipe() -> dict:
    return {
        "$schema": "modelfiche.checkpoint-merge/v1",
        "name": "Modern Times full3x coverage",
        "operator": "rank_concat",
        "base_model": "krea/Krea-2-Raw",
        "inputs": [
            {
                "alias": alias,
                "checkpoint_revision_id": revision_id,
                "sha256": content_sha,
                "step": step,
                "rank": rank,
                "role": role,
                "key_format": key_format,
                "weights": {"text_fusion": text_weight, "transformer": transformer_weight},
            }
            for alias, revision_id, content_sha, step, rank, role, text_weight, transformer_weight, key_format in SOURCES
        ],
        "output": {"rank": 160, "dtype": "float16"},
    }


def upgrade() -> None:
    bind = op.get_bind()
    run = bind.execute(sa.text("SELECT p.workspace_id, r.project_id FROM training_runs r JOIN projects p ON p.id = r.project_id WHERE r.id = :id"), {"id": RUN_ID}).first()
    if run is None:
        return
    source_count = bind.execute(sa.text("SELECT count(*) FROM checkpoint_revisions WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True)), {"ids": [source[1] for source in SOURCES]}).scalar_one()
    output_exists = bind.execute(sa.text("SELECT 1 FROM checkpoint_revisions WHERE id = :id"), {"id": OUTPUT_REVISION_ID}).first()
    if source_count != len(SOURCES) or output_exists is None:
        return
    existing = bind.execute(sa.text("SELECT id FROM merge_operations WHERE run_id = :run_id"), {"run_id": RUN_ID}).first()
    if existing:
        return
    recipe = _recipe()
    canonical = json.dumps(recipe, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    operation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    bind.execute(sa.text("""
        INSERT INTO merge_operations
            (workspace_id, project_id, run_id, schema_version, operator, base_model, status, recipe, recipe_digest, compact_notation, output_rank, dtype, output_checkpoint_revision_id, error_code, error_message, failure_phase, last_heartbeat_at, created_at, updated_at, id)
        VALUES
            (:workspace_id, :project_id, :run_id, 'modelfiche.checkpoint-merge/v1', 'rank_concat', 'krea/Krea-2-Raw', 'completed', :recipe, :digest, :notation, 160, 'float16', :output_revision_id, NULL, NULL, NULL, :now, :now, :now, :id)
    """), {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "run_id": RUN_ID,
        "recipe": canonical,
        "digest": digest,
        "notation": "rank-concat[r160] full@2400 + render@3000 + flat@3250 -> full3x",
        "output_revision_id": OUTPUT_REVISION_ID,
        "now": now,
        "id": operation_id,
    })
    for position, source in enumerate(SOURCES):
        alias, revision_id, _content_sha, _step, rank, role, text_weight, transformer_weight, _key_format = source
        input_id = str(uuid.uuid4())
        bind.execute(sa.text("""
            INSERT INTO merge_inputs
                (merge_operation_id, checkpoint_revision_id, alias, role, source_rank, position, created_at, updated_at, id)
            VALUES (:operation_id, :revision_id, :alias, :role, :rank, :position, :now, :now, :id)
        """), {"operation_id": operation_id, "revision_id": revision_id, "alias": alias, "role": role, "rank": rank, "position": position, "now": now, "id": input_id})
        for scope, weight in (("text_fusion", text_weight), ("transformer", transformer_weight)):
            bind.execute(sa.text("""
                INSERT INTO merge_input_weights
                    (merge_input_id, scope, module_pattern, weight, created_at, updated_at, id)
                VALUES (:input_id, :scope, NULL, :weight, :now, :now, :id)
            """), {"input_id": input_id, "scope": scope, "weight": weight, "now": now, "id": str(uuid.uuid4())})
        edge = bind.execute(sa.text("""
            SELECT id FROM lineage_edges
            WHERE workspace_id = :workspace_id AND source_type = 'checkpoint_revision' AND source_id = :source_id
              AND target_type = 'checkpoint_revision' AND target_id = :target_id AND relationship = 'merged_into'
        """), {"workspace_id": run.workspace_id, "source_id": revision_id, "target_id": OUTPUT_REVISION_ID}).first()
        if edge is None:
            bind.execute(sa.text("""
                INSERT INTO lineage_edges
                    (workspace_id, source_type, source_id, target_type, target_id, relationship, metadata, created_at, updated_at, id)
                VALUES (:workspace_id, 'checkpoint_revision', :source_id, 'checkpoint_revision', :target_id, 'merged_into', :metadata, :now, :now, :id)
            """), {"workspace_id": run.workspace_id, "source_id": revision_id, "target_id": OUTPUT_REVISION_ID, "metadata": json.dumps({"merge_operation_id": operation_id, "operator": "rank_concat"}), "now": now, "id": str(uuid.uuid4())})


def downgrade() -> None:
    bind = op.get_bind()
    operation = bind.execute(sa.text("SELECT id FROM merge_operations WHERE run_id = :run_id"), {"run_id": RUN_ID}).first()
    if operation is None:
        return
    bind.execute(sa.text("""
        DELETE FROM lineage_edges
        WHERE source_type = 'checkpoint_revision' AND source_id IN :source_ids
          AND target_type = 'checkpoint_revision' AND target_id = :target_id AND relationship = 'merged_into'
    """).bindparams(sa.bindparam("source_ids", expanding=True)), {"source_ids": [source[1] for source in SOURCES], "target_id": OUTPUT_REVISION_ID})
    bind.execute(sa.text("DELETE FROM merge_operations WHERE id = :id"), {"id": operation.id})
