"""Reclassify imported training runs by their project trigger.

Revision ID: 0013_reclassify_runs_by_trigger
Revises: 0012_backfill_sample_project_ownership
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0013_reclassify_runs_by_trigger"
down_revision = "0012_backfill_sample_project_ownership"
branch_labels = None
depends_on = None


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _project_for_run(projects: list[dict[str, Any]], run: dict[str, Any]) -> str | None:
    evidence = " ".join(
        str(value or "") for value in (run["source_prefix"], run["name"], json.dumps(_json(run["normalized_config"], {}), sort_keys=True))
    ).casefold()
    matches = []
    for project in projects:
        triggers = [str(trigger).strip().casefold() for trigger in _json(project["trigger_words"], []) if str(trigger).strip()]
        if any(re.search(rf"(?<![a-z0-9]){re.escape(trigger)}(?![a-z0-9])", evidence) for trigger in triggers):
            matches.append(project["id"])
    return matches[0] if len(matches) == 1 else None


def upgrade():
    bind = op.get_bind()
    metadata = sa.MetaData()
    projects = sa.Table("projects", metadata, autoload_with=bind)
    runs = sa.Table("training_runs", metadata, autoload_with=bind)
    samples = sa.Table("samples", metadata, autoload_with=bind)
    checkpoints = sa.Table("checkpoints", metadata, autoload_with=bind)
    assets = sa.Table("assets", metadata, autoload_with=bind)
    datasets = sa.Table("datasets", metadata, autoload_with=bind)
    dataset_versions = sa.Table("dataset_versions", metadata, autoload_with=bind)
    model_versions = sa.Table("model_versions", metadata, autoload_with=bind)
    models = sa.Table("models", metadata, autoload_with=bind)
    import_jobs = sa.Table("import_jobs", metadata, autoload_with=bind)
    activity_events = sa.Table("activity_events", metadata, autoload_with=bind)

    projects_by_workspace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bind.execute(sa.select(projects.c.id, projects.c.workspace_id, projects.c.trigger_words).where(projects.c.state != "archived")).mappings():
        projects_by_workspace[row["workspace_id"]].append(dict(row))

    moved_runs: list[tuple[dict[str, Any], str]] = []
    for run in bind.execute(sa.select(runs)).mappings():
        project = bind.execute(sa.select(projects.c.workspace_id).where(projects.c.id == run["project_id"])).mappings().one_or_none()
        if project is None:
            continue
        target_project_id = _project_for_run(projects_by_workspace[project["workspace_id"]], dict(run))
        if target_project_id and target_project_id != run["project_id"]:
            moved_runs.append((dict(run), target_project_id))

    for run, target_project_id in moved_runs:
        run_id = run["id"]
        bind.execute(runs.update().where(runs.c.id == run_id).values(project_id=target_project_id))
        asset_ids = set(bind.execute(sa.select(samples.c.asset_id).where(samples.c.run_id == run_id)).scalars())
        asset_ids.update(bind.execute(sa.select(checkpoints.c.asset_id).where(checkpoints.c.run_id == run_id)).scalars())
        if asset_ids:
            bind.execute(assets.update().where(assets.c.id.in_(asset_ids)).values(project_id=target_project_id))
        bind.execute(import_jobs.update().where(
            import_jobs.c.source_id == run["origin_source_id"],
            import_jobs.c.prefix == run["source_prefix"],
        ).values(project_id=target_project_id))
        bind.execute(activity_events.update().where(
            activity_events.c.subject_type == "training_run",
            activity_events.c.subject_id == run_id,
        ).values(project_id=target_project_id))

        dataset_id = bind.execute(
            sa.select(dataset_versions.c.dataset_id).where(dataset_versions.c.id == run["dataset_version_id"])
        ).scalar_one_or_none() if run["dataset_version_id"] else None
        if dataset_id:
            linked_projects = set(bind.execute(
                sa.select(runs.c.project_id)
                .join(dataset_versions, dataset_versions.c.id == runs.c.dataset_version_id)
                .where(dataset_versions.c.dataset_id == dataset_id)
            ).scalars())
            if linked_projects == {target_project_id}:
                bind.execute(datasets.update().where(datasets.c.id == dataset_id).values(project_id=target_project_id))

        model_ids = set(bind.execute(
            sa.select(model_versions.c.model_id)
            .join(checkpoints, checkpoints.c.id == model_versions.c.checkpoint_id)
            .where(checkpoints.c.run_id == run_id)
        ).scalars())
        for model_id in model_ids:
            model_projects = set(bind.execute(
                sa.select(runs.c.project_id)
                .join(checkpoints, checkpoints.c.run_id == runs.c.id)
                .join(model_versions, model_versions.c.checkpoint_id == checkpoints.c.id)
                .where(model_versions.c.model_id == model_id)
            ).scalars())
            if model_projects == {target_project_id}:
                bind.execute(models.update().where(models.c.id == model_id).values(project_id=target_project_id))


def downgrade():
    # Source-trigger evidence is the authoritative replacement for prior manual assignment.
    pass
