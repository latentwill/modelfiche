from __future__ import annotations

import json
import os
import tempfile
import zipfile
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models

from .runner import JobContext


def _dump(row) -> dict[str, Any]:
    return {attribute.columns[0].name: getattr(row, attribute.key) for attribute in sa_inspect(row).mapper.column_attrs}


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(type(value).__name__)


class ExportPackageHandler:
    def __init__(self, session_factory: sessionmaker[Session], export_root: Path, allowed_file_roots: tuple[Path, ...]):
        self.session_factory = session_factory
        self.export_root = export_root.resolve()
        self.allowed_file_roots = tuple(root.resolve() for root in allowed_file_roots)
        self.export_root.mkdir(parents=True, exist_ok=True)

    def __call__(self, context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        with self.session_factory() as session:
            manifest, assets = self._collect(session, payload)
        filename = f"titles-export-{context.job_id}.zip"
        destination = self.export_root / filename
        fd, raw_path = tempfile.mkstemp(prefix="export-", suffix=".zip", dir=self.export_root)
        os.close(fd)
        temp_path = Path(raw_path)
        included_files = 0
        try:
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, default=_json_default, indent=2, sort_keys=True))
                if bool(payload.get("include_files", True)):
                    for index, asset in enumerate(assets):
                        if context.cancellation_requested():
                            return self._cancel(temp_path)
                        local_path = self._local_file(asset["locations"])
                        if local_path:
                            archive.write(local_path, f"files/{asset['id']}/{Path(asset['name']).name}")
                            included_files += 1
                        context.progress(0.1 + 0.8 * ((index + 1) / max(len(assets), 1)))
            os.replace(temp_path, destination)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return {
            "path": str(destination), "filename": filename, "asset_count": len(assets),
            "included_files": included_files, "manifest_only_assets": len(assets) - included_files,
        }

    @staticmethod
    def _cancel(temp_path: Path) -> dict[str, Any]:
        temp_path.unlink(missing_ok=True)
        return {"canceled": True}

    def _local_file(self, locations: list[dict[str, Any]]) -> Path | None:
        for location in locations:
            if location["provider"] != "local" or location["hydration_state"] != "hydrated":
                continue
            path = Path(location["uri"]).expanduser().resolve()
            if path.is_file() and any(path == root or root in path.parents for root in self.allowed_file_roots):
                return path
        return None

    def _collect(self, session: Session, payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        asset_ids = {str(item) for item in payload.get("asset_ids", [])}
        dataset_version_ids = {str(item) for item in payload.get("dataset_version_ids", [])}
        model_version_ids = {str(item) for item in payload.get("model_version_ids", [])}
        eval_run_ids = {str(item) for item in payload.get("eval_run_ids", [])}
        selected_project_id = str(payload["project_id"]) if payload.get("project_id") else None

        project_ids: set[str] = set()
        dataset_ids: set[str] = set()
        dataset_item_ids: set[str] = set()
        training_run_ids: set[str] = set()
        training_stage_ids: set[str] = set()
        checkpoint_ids: set[str] = set()
        sample_ids: set[str] = set()
        model_ids: set[str] = set()
        prompt_set_ids: set[str] = set()
        prompt_ids: set[str] = set()
        eval_definition_ids: set[str] = set()
        eval_output_ids: set[str] = set()
        grid_definition_ids: set[str] = set()
        grid_cell_ids: set[str] = set()

        def values(statement) -> set[str]:
            return {str(value) for value in session.scalars(statement) if value is not None}

        if selected_project_id:
            project_ids.add(selected_project_id)
            asset_ids.update(values(select(models.Asset.id).where(models.Asset.project_id == selected_project_id)))
            dataset_ids.update(values(select(models.Dataset.id).where(models.Dataset.project_id == selected_project_id)))
            training_run_ids.update(values(select(models.TrainingRun.id).where(models.TrainingRun.project_id == selected_project_id)))
            model_ids.update(values(select(models.Model.id).where(models.Model.project_id == selected_project_id)))
            prompt_set_ids.update(values(select(models.PromptSet.id).where(models.PromptSet.project_id == selected_project_id)))
            eval_definition_ids.update(values(select(models.EvalDefinition.id).where(models.EvalDefinition.project_id == selected_project_id)))
            grid_definition_ids.update(values(select(models.GridDefinition.id).where(models.GridDefinition.project_id == selected_project_id)))
            if dataset_ids:
                dataset_version_ids.update(values(select(models.DatasetVersion.id).where(models.DatasetVersion.dataset_id.in_(dataset_ids))))
            if model_ids:
                model_version_ids.update(values(select(models.ModelVersion.id).where(models.ModelVersion.model_id.in_(model_ids))))

        # Explicit version/run selections expand only their own lineage. Ancestor
        # projects are context records, not an instruction to export the project.
        if dataset_version_ids:
            dataset_ids.update(values(select(models.DatasetVersion.dataset_id).where(models.DatasetVersion.id.in_(dataset_version_ids))))
        if model_version_ids:
            model_ids.update(values(select(models.ModelVersion.model_id).where(models.ModelVersion.id.in_(model_version_ids))))
            checkpoint_ids.update(values(select(models.ModelVersion.checkpoint_id).where(models.ModelVersion.id.in_(model_version_ids))))
        if eval_run_ids:
            eval_definition_ids.update(values(select(models.EvalRun.definition_id).where(models.EvalRun.id.in_(eval_run_ids))))
            checkpoint_ids.update(values(select(models.EvalRun.checkpoint_id).where(models.EvalRun.id.in_(eval_run_ids))))

        if eval_definition_ids:
            prompt_set_ids.update(values(select(models.EvalDefinition.prompt_set_id).where(models.EvalDefinition.id.in_(eval_definition_ids))))
            model_version_ids.update(values(select(models.EvalDefinition.model_version_id).where(models.EvalDefinition.id.in_(eval_definition_ids))))
        if model_version_ids:
            model_ids.update(values(select(models.ModelVersion.model_id).where(models.ModelVersion.id.in_(model_version_ids))))
            checkpoint_ids.update(values(select(models.ModelVersion.checkpoint_id).where(models.ModelVersion.id.in_(model_version_ids))))
        if checkpoint_ids:
            training_run_ids.update(values(select(models.Checkpoint.run_id).where(models.Checkpoint.id.in_(checkpoint_ids))))

        # A selected training lineage includes its configuration, checkpoints,
        # samples, and source dataset so a model/eval handoff is reproducible.
        if training_run_ids:
            dataset_version_ids.update(values(select(models.TrainingRun.dataset_version_id).where(models.TrainingRun.id.in_(training_run_ids))))
            training_stage_ids.update(values(select(models.TrainingStage.id).where(models.TrainingStage.run_id.in_(training_run_ids))))
            checkpoint_ids.update(values(select(models.Checkpoint.id).where(models.Checkpoint.run_id.in_(training_run_ids))))
            sample_ids.update(values(select(models.Sample.id).where(models.Sample.run_id.in_(training_run_ids))))
        if checkpoint_ids:
            asset_ids.update(values(select(models.Checkpoint.asset_id).where(models.Checkpoint.id.in_(checkpoint_ids))))
        if sample_ids:
            asset_ids.update(values(select(models.Sample.asset_id).where(models.Sample.id.in_(sample_ids))))

        if dataset_version_ids:
            dataset_ids.update(values(select(models.DatasetVersion.dataset_id).where(models.DatasetVersion.id.in_(dataset_version_ids))))
            dataset_item_ids.update(values(select(models.DatasetItem.id).where(models.DatasetItem.dataset_version_id.in_(dataset_version_ids))))
        if dataset_item_ids:
            asset_ids.update(values(select(models.DatasetItem.asset_id).where(models.DatasetItem.id.in_(dataset_item_ids))))

        if prompt_set_ids:
            prompt_ids.update(values(select(models.Prompt.id).where(models.Prompt.prompt_set_id.in_(prompt_set_ids))))
        if eval_definition_ids:
            if selected_project_id:
                eval_run_ids.update(values(select(models.EvalRun.id).where(models.EvalRun.definition_id.in_(eval_definition_ids))))
        if eval_run_ids:
            eval_output_ids.update(values(select(models.EvalOutput.id).where(models.EvalOutput.eval_run_id.in_(eval_run_ids))))
        if eval_output_ids:
            asset_ids.update(values(select(models.EvalOutput.asset_id).where(models.EvalOutput.id.in_(eval_output_ids))))

        if selected_project_id and grid_definition_ids:
            grid_cell_ids.update(values(select(models.GridCell.id).where(models.GridCell.grid_definition_id.in_(grid_definition_ids))))
        elif eval_output_ids:
            grid_cell_ids.update(values(select(models.GridCell.id).where(models.GridCell.eval_output_id.in_(eval_output_ids))))
            grid_definition_ids.update(values(select(models.GridCell.grid_definition_id).where(models.GridCell.id.in_(grid_cell_ids))))

        if dataset_ids:
            project_ids.update(values(select(models.Dataset.project_id).where(models.Dataset.id.in_(dataset_ids))))
        if training_run_ids:
            project_ids.update(values(select(models.TrainingRun.project_id).where(models.TrainingRun.id.in_(training_run_ids))))
        if model_ids:
            project_ids.update(values(select(models.Model.project_id).where(models.Model.id.in_(model_ids))))
        if prompt_set_ids:
            project_ids.update(values(select(models.PromptSet.project_id).where(models.PromptSet.id.in_(prompt_set_ids))))
        if eval_definition_ids:
            project_ids.update(values(select(models.EvalDefinition.project_id).where(models.EvalDefinition.id.in_(eval_definition_ids))))
        if asset_ids:
            project_ids.update(values(select(models.Asset.project_id).where(models.Asset.id.in_(asset_ids))))

        def rows(model, ids: set[str]) -> list[dict[str, Any]]:
            if not ids:
                return []
            statement = select(model).where(model.id.in_(ids)).order_by(model.created_at, model.id)
            return [_dump(row) for row in session.scalars(statement)]

        projects = rows(models.Project, project_ids)
        datasets = rows(models.Dataset, dataset_ids)
        dataset_versions = rows(models.DatasetVersion, dataset_version_ids)
        dataset_items = rows(models.DatasetItem, dataset_item_ids)
        training_runs = rows(models.TrainingRun, training_run_ids)
        training_stages = rows(models.TrainingStage, training_stage_ids)
        checkpoints = rows(models.Checkpoint, checkpoint_ids)
        samples = rows(models.Sample, sample_ids)
        model_rows = rows(models.Model, model_ids)
        model_versions = rows(models.ModelVersion, model_version_ids)
        prompt_sets = rows(models.PromptSet, prompt_set_ids)
        prompts = rows(models.Prompt, prompt_ids)
        eval_definitions = rows(models.EvalDefinition, eval_definition_ids)
        eval_runs = rows(models.EvalRun, eval_run_ids)
        eval_outputs = rows(models.EvalOutput, eval_output_ids)
        grid_definitions = rows(models.GridDefinition, grid_definition_ids)
        grid_cells = rows(models.GridCell, grid_cell_ids)

        assets = []
        for asset in session.scalars(select(models.Asset).where(models.Asset.id.in_(asset_ids)).order_by(models.Asset.created_at, models.Asset.id)) if asset_ids else []:
            locations = list(session.scalars(
                select(models.AssetLocation).where(models.AssetLocation.asset_id == asset.id).order_by(models.AssetLocation.created_at, models.AssetLocation.id)
            ))
            image_metadata = session.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
            assets.append({
                **_dump(asset),
                "locations": [_dump(location) for location in locations],
                "image_metadata": _dump(image_metadata) if image_metadata else None,
            })

        entity_rows = {
            "project": projects, "asset": assets, "dataset": datasets,
            "dataset_version": dataset_versions, "dataset_item": dataset_items,
            "training_run": training_runs, "training_stage": training_stages,
            "checkpoint": checkpoints, "sample": samples, "model": model_rows,
            "model_version": model_versions, "prompt_set": prompt_sets, "prompt": prompts,
            "eval_definition": eval_definitions, "eval_run": eval_runs, "eval_output": eval_outputs,
            "grid_definition": grid_definitions, "grid_cell": grid_cells,
        }
        subjects = {(subject_type, row["id"]) for subject_type, subject_rows in entity_rows.items() for row in subject_rows}
        reviews, comments = [], []
        if bool(payload.get("include_reviews", True)) and subjects:
            subject_types = {item[0] for item in subjects}
            subject_ids = {item[1] for item in subjects}
            review_rows = session.scalars(select(models.Review).where(
                models.Review.subject_type.in_(subject_types), models.Review.subject_id.in_(subject_ids)
            ).order_by(models.Review.created_at, models.Review.id))
            comment_rows = session.scalars(select(models.Comment).where(
                models.Comment.subject_type.in_(subject_types), models.Comment.subject_id.in_(subject_ids)
            ).order_by(models.Comment.created_at, models.Comment.id))
            reviews = [_dump(row) for row in review_rows if (row.subject_type, row.subject_id) in subjects]
            comments = [_dump(row) for row in comment_rows if (row.subject_type, row.subject_id) in subjects]
        manifest = {
            "format": "titles-dam-export", "version": 2, "created_at": datetime.now().astimezone(),
            "selection": {
                "project_id": selected_project_id,
                "asset_ids": sorted(str(item) for item in payload.get("asset_ids", [])),
                "dataset_version_ids": sorted(str(item) for item in payload.get("dataset_version_ids", [])),
                "model_version_ids": sorted(str(item) for item in payload.get("model_version_ids", [])),
                "eval_run_ids": sorted(str(item) for item in payload.get("eval_run_ids", [])),
            },
            "projects": projects, "assets": assets, "datasets": datasets,
            "dataset_versions": dataset_versions, "dataset_items": dataset_items,
            "training_runs": training_runs, "training_stages": training_stages,
            "checkpoints": checkpoints, "samples": samples,
            "models": model_rows, "model_versions": model_versions,
            "prompt_sets": prompt_sets, "prompts": prompts,
            "eval_definitions": eval_definitions, "eval_runs": eval_runs, "eval_outputs": eval_outputs,
            "grid_definitions": grid_definitions, "grid_cells": grid_cells,
            "reviews": reviews, "comments": comments,
        }
        return manifest, assets
