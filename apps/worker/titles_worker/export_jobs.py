from __future__ import annotations

import hashlib
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
from titles_api.integrations.config import S3Settings
from titles_api.storage.s3_client import create_source_s3_client

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
        filename = f"titles-export-{context.job_id}.zip"
        destination = self.export_root / filename
        fd, raw_path = tempfile.mkstemp(prefix="export-", suffix=".zip", dir=self.export_root)
        os.close(fd)
        temp_path = Path(raw_path)
        included_files = 0
        asset_count = 0
        try:
            with self.session_factory() as session:
                selection = self._selection(session, payload)
                with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    asset_count = self._write_manifest(
                        archive,
                        session,
                        selection,
                        payload,
                        context,
                    )
                    if context.cancellation_requested():
                        return self._cancel(temp_path)
                    if bool(payload.get("include_files", True)):
                        for index, asset in enumerate(self._iter_assets(session, selection["asset_ids"])):
                            if context.cancellation_requested():
                                return self._cancel(temp_path)
                            local_path = self._local_file(asset["locations"])
                            archive_name = f"files/{asset['id']}/{Path(asset['name']).name}"
                            if local_path:
                                archive.write(local_path, archive_name)
                                included_files += 1
                            else:
                                if not self._write_remote_file(archive, session, asset, archive_name, context):
                                    return self._cancel(temp_path)
                                included_files += 1
                            context.progress(0.1 + 0.8 * ((index + 1) / max(asset_count, 1)))
            os.replace(temp_path, destination)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return {
            "path": str(destination),
            "filename": filename,
            "asset_count": asset_count,
            "included_files": included_files,
            "manifest_only_assets": asset_count - included_files,
        }

    @staticmethod
    def _cancel(temp_path: Path) -> dict[str, Any]:
        temp_path.unlink(missing_ok=True)
        return {"canceled": True}

    def _local_file(self, locations: list[dict[str, Any]]) -> Path | None:
        for location in locations:
            if location["provider"] != "local" or location["hydration_state"] != "hydrated":
                continue
            uri = location.get("uri")
            if not isinstance(uri, str) or not uri:
                continue
            path = Path(uri).resolve()
            if not path.is_file():
                continue
            if any(root == path or root in path.parents for root in self.allowed_file_roots):
                return path
        return None

    @staticmethod
    def _remote_candidate(session: Session, asset: dict[str, Any]) -> tuple[dict[str, Any], models.ImportSource] | None:
        expected_sha256 = str(asset.get("sha256") or "").lower()
        if not expected_sha256:
            return None
        for location in reversed(asset["locations"]):
            if (
                location.get("provider") != "s3"
                or location.get("verification_state") not in {"verified", "available"}
                or str(location.get("verified_sha256") or "").lower() != expected_sha256
            ):
                continue
            source_id = location.get("source_id")
            source = session.get(models.ImportSource, str(source_id)) if source_id else None
            if source is None or source.provider != "s3" or not source.is_active:
                continue
            bucket = location.get("bucket") or source.bucket
            object_key = location.get("object_key")
            if (
                not bucket
                or bucket != source.bucket
                or not isinstance(object_key, str)
                or not object_key
            ):
                continue
            allowed = tuple(
                str(prefix).strip("/")
                for prefix in (source.allowed_prefixes or ())
                if str(prefix).strip("/")
            )
            if allowed and not any(
                object_key == prefix or object_key.startswith(f"{prefix}/")
                for prefix in allowed
            ):
                continue
            return location, source
        return None

    def _write_remote_file(
        self,
        archive: zipfile.ZipFile,
        session: Session,
        asset: dict[str, Any],
        archive_name: str,
        context: JobContext,
    ) -> bool:
        candidate = self._remote_candidate(session, asset)
        if candidate is None:
            raise LookupError(
                f"asset {asset['id']} has no usable local file or verified S3 location; "
                "hydrate the asset or configure an active verified S3 source"
            )
        location, source = candidate
        try:
            settings = S3Settings.for_source(
                endpoint_url=source.endpoint_url,
                bucket=source.bucket,
                allowed_prefixes=tuple(source.allowed_prefixes or ()),
                region=source.region,
                addressing_style=source.addressing_style,
                credential_env_prefix=source.credential_env_prefix,
            )
            client = create_source_s3_client(settings)
        except Exception as exc:
            raise RuntimeError(
                f"cannot export asset {asset['id']}: S3 credentials are unavailable for "
                f"import source {source.name!r}; configure "
                f"{source.credential_env_prefix.upper()}_ACCESS_KEY and "
                f"{source.credential_env_prefix.upper()}_SECRET_KEY"
            ) from exc
        request: dict[str, Any] = {"Bucket": source.bucket, "Key": location["object_key"]}
        if location.get("etag"):
            request["IfMatch"] = location["etag"]
        if location.get("version_id"):
            request["VersionId"] = location["version_id"]
        body = None
        try:
            response = client.get_object(**request)
            body = response["Body"]
            expected_size = location.get("verified_size")
            if expected_size is None:
                expected_size = location.get("size")
            content_length = response.get("ContentLength")
            if expected_size is not None and content_length is not None and int(content_length) != int(expected_size):
                raise RuntimeError(
                    f"cannot export asset {asset['id']}: S3 object size does not match verified metadata"
                )
            digest = hashlib.sha256()
            total = 0
            with archive.open(archive_name, "w") as output:
                while True:
                    if context.cancellation_requested():
                        return False
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise RuntimeError(f"cannot export asset {asset['id']}: S3 returned a non-byte body")
                    raw = bytes(chunk)
                    total += len(raw)
                    if expected_size is not None and total > int(expected_size):
                        raise RuntimeError(
                            f"cannot export asset {asset['id']}: S3 object exceeds verified metadata size"
                        )
                    digest.update(raw)
                    output.write(raw)
            if expected_size is not None and total != int(expected_size):
                raise RuntimeError(f"cannot export asset {asset['id']}: S3 object size changed during read")
            if digest.hexdigest() != str(asset["sha256"]).lower():
                raise RuntimeError(f"cannot export asset {asset['id']}: S3 object digest differs from the asset")
            return True
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"cannot export asset {asset['id']}: S3 object could not be read from "
                f"import source {source.name!r}"
            ) from exc
        finally:
            close = getattr(body, "close", None)
            if close:
                close()

    def _selection(self, session: Session, payload: dict[str, Any]) -> dict[str, Any]:
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
        if eval_definition_ids and selected_project_id:
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
        return {
            "project_ids": project_ids,
            "asset_ids": asset_ids,
            "dataset_ids": dataset_ids,
            "dataset_version_ids": dataset_version_ids,
            "dataset_item_ids": dataset_item_ids,
            "training_run_ids": training_run_ids,
            "training_stage_ids": training_stage_ids,
            "checkpoint_ids": checkpoint_ids,
            "sample_ids": sample_ids,
            "model_ids": model_ids,
            "model_version_ids": model_version_ids,
            "prompt_set_ids": prompt_set_ids,
            "prompt_ids": prompt_ids,
            "eval_definition_ids": eval_definition_ids,
            "eval_run_ids": eval_run_ids,
            "eval_output_ids": eval_output_ids,
            "grid_definition_ids": grid_definition_ids,
            "grid_cell_ids": grid_cell_ids,
            "selected_project_id": selected_project_id,
        }

    def _iter_rows(self, session: Session, model, ids: set[str]):
        if not ids:
            return iter(())
        statement = select(model).where(model.id.in_(ids)).order_by(model.created_at, model.id)
        return (_dump(row) for row in session.scalars(statement).yield_per(500))

    def _iter_assets(self, session: Session, ids: set[str]):
        if not ids:
            return iter(())
        statement = select(models.Asset).where(models.Asset.id.in_(ids)).order_by(models.Asset.created_at, models.Asset.id)
        def rows():
            for asset in session.scalars(statement).yield_per(500):
                locations = session.scalars(
                    select(models.AssetLocation)
                    .where(models.AssetLocation.asset_id == asset.id)
                    .order_by(models.AssetLocation.created_at, models.AssetLocation.id)
                )
                image_metadata = session.scalar(select(models.ImageMetadata).where(models.ImageMetadata.asset_id == asset.id))
                yield {
                    **_dump(asset),
                    "locations": [_dump(location) for location in locations],
                    "image_metadata": _dump(image_metadata) if image_metadata else None,
                }
        return rows()

    def _write_manifest(self, archive: zipfile.ZipFile, session: Session, selection: dict[str, Any], payload: dict[str, Any], context: JobContext) -> int:
        entity_models = (
            ("projects", models.Project, "project_ids"),
            ("datasets", models.Dataset, "dataset_ids"),
            ("dataset_versions", models.DatasetVersion, "dataset_version_ids"),
            ("dataset_items", models.DatasetItem, "dataset_item_ids"),
            ("training_runs", models.TrainingRun, "training_run_ids"),
            ("training_stages", models.TrainingStage, "training_stage_ids"),
            ("checkpoints", models.Checkpoint, "checkpoint_ids"),
            ("samples", models.Sample, "sample_ids"),
            ("models", models.Model, "model_ids"),
            ("model_versions", models.ModelVersion, "model_version_ids"),
            ("prompt_sets", models.PromptSet, "prompt_set_ids"),
            ("prompts", models.Prompt, "prompt_ids"),
            ("eval_definitions", models.EvalDefinition, "eval_definition_ids"),
            ("eval_runs", models.EvalRun, "eval_run_ids"),
            ("eval_outputs", models.EvalOutput, "eval_output_ids"),
            ("grid_definitions", models.GridDefinition, "grid_definition_ids"),
            ("grid_cells", models.GridCell, "grid_cell_ids"),
        )
        subject_ids = {
            kind: selection[f"{kind}_ids"]
            for kind in ("project", "asset", "dataset", "dataset_version", "dataset_item", "training_run", "training_stage", "checkpoint", "sample", "model", "model_version", "prompt_set", "prompt", "eval_definition", "eval_run", "eval_output", "grid_definition", "grid_cell")
        }
        subjects = {(kind, item) for kind, ids in subject_ids.items() for item in ids}
        with archive.open("manifest.json", "w") as stream:
            stream.write(b"{")
            first = True
            def field(name: str, value: Any):
                nonlocal first
                if not first:
                    stream.write(b",")
                first = False
                stream.write(json.dumps(name).encode())
                stream.write(b":")
                stream.write(json.dumps(value, default=_json_default, sort_keys=True).encode())
            field("format", "titles-dam-export")
            field("version", 2)
            field("created_at", datetime.now().astimezone())
            field("selection", {
                "project_id": selection["selected_project_id"],
                "asset_ids": sorted(str(item) for item in payload.get("asset_ids", [])),
                "dataset_version_ids": sorted(str(item) for item in payload.get("dataset_version_ids", [])),
                "model_version_ids": sorted(str(item) for item in payload.get("model_version_ids", [])),
                "eval_run_ids": sorted(str(item) for item in payload.get("eval_run_ids", [])),
            })
            asset_count = 0
            def array_field(name: str, rows) -> int:
                nonlocal first
                if not first:
                    stream.write(b",")
                first = False
                stream.write(json.dumps(name).encode() + b":[")
                count = 0
                for row in rows:
                    if count:
                        stream.write(b",")
                    stream.write(json.dumps(row, default=_json_default, sort_keys=True).encode())
                    count += 1
                stream.write(b"]")
                return count
            asset_count = array_field("projects", self._iter_rows(session, models.Project, selection["project_ids"]))
            for name, model, key in entity_models[1:]:
                if context.cancellation_requested():
                    return asset_count
                if name == "assets":
                    continue
                array_field(name, self._iter_rows(session, model, selection[key]))
            asset_count = array_field("assets", self._iter_assets(session, selection["asset_ids"]))
            if bool(payload.get("include_reviews", True)) and subjects:
                subject_types = {item[0] for item in subjects}
                subject_values = {item[1] for item in subjects}
                reviews = session.scalars(select(models.Review).where(models.Review.subject_type.in_(subject_types), models.Review.subject_id.in_(subject_values)).order_by(models.Review.created_at, models.Review.id))
                comments = session.scalars(select(models.Comment).where(models.Comment.subject_type.in_(subject_types), models.Comment.subject_id.in_(subject_values)).order_by(models.Comment.created_at, models.Comment.id))
                array_field("reviews", (_dump(row) for row in reviews.yield_per(500) if (row.subject_type, row.subject_id) in subjects))
                array_field("comments", (_dump(row) for row in comments.yield_per(500) if (row.subject_type, row.subject_id) in subjects))
            else:
                array_field("reviews", ())
                array_field("comments", ())
            stream.write(b"}")
        return asset_count
    def _collect(self, session: Session, payload: dict[str, Any]):
        """Materialize a manifest for callers that inspect an export before writing."""
        selection = self._selection(session, payload)
        rows = {
            name: list(self._iter_rows(session, model, selection[key]))
            for name, model, key in (
                ("projects", models.Project, "project_ids"),
                ("datasets", models.Dataset, "dataset_ids"),
                ("dataset_versions", models.DatasetVersion, "dataset_version_ids"),
                ("dataset_items", models.DatasetItem, "dataset_item_ids"),
                ("training_runs", models.TrainingRun, "training_run_ids"),
                ("training_stages", models.TrainingStage, "training_stage_ids"),
                ("checkpoints", models.Checkpoint, "checkpoint_ids"),
                ("samples", models.Sample, "sample_ids"),
                ("models", models.Model, "model_ids"),
                ("model_versions", models.ModelVersion, "model_version_ids"),
                ("prompt_sets", models.PromptSet, "prompt_set_ids"),
                ("prompts", models.Prompt, "prompt_ids"),
                ("eval_definitions", models.EvalDefinition, "eval_definition_ids"),
                ("eval_runs", models.EvalRun, "eval_run_ids"),
                ("eval_outputs", models.EvalOutput, "eval_output_ids"),
                ("grid_definitions", models.GridDefinition, "grid_definition_ids"),
                ("grid_cells", models.GridCell, "grid_cell_ids"),
            )
        }
        assets = list(self._iter_assets(session, selection["asset_ids"]))
        rows["assets"] = assets
        subjects = {(kind, item) for kind, ids in (
            ("project", selection["project_ids"]), ("asset", selection["asset_ids"]),
            ("dataset", selection["dataset_ids"]), ("dataset_version", selection["dataset_version_ids"]),
            ("dataset_item", selection["dataset_item_ids"]), ("training_run", selection["training_run_ids"]),
            ("training_stage", selection["training_stage_ids"]), ("checkpoint", selection["checkpoint_ids"]),
            ("sample", selection["sample_ids"]), ("model", selection["model_ids"]),
            ("model_version", selection["model_version_ids"]), ("prompt_set", selection["prompt_set_ids"]),
            ("prompt", selection["prompt_ids"]), ("eval_definition", selection["eval_definition_ids"]),
            ("eval_run", selection["eval_run_ids"]), ("eval_output", selection["eval_output_ids"]),
            ("grid_definition", selection["grid_definition_ids"]), ("grid_cell", selection["grid_cell_ids"]),
        ) for item in ids}
        rows["reviews"] = []
        rows["comments"] = []
        if bool(payload.get("include_reviews", True)) and subjects:
            types = {kind for kind, _ in subjects}
            ids = {item for _, item in subjects}
            rows["reviews"] = [
                _dump(row) for row in session.scalars(
                    select(models.Review).where(models.Review.subject_type.in_(types), models.Review.subject_id.in_(ids)).order_by(models.Review.created_at, models.Review.id)
                ) if (row.subject_type, row.subject_id) in subjects
            ]
            rows["comments"] = [
                _dump(row) for row in session.scalars(
                    select(models.Comment).where(models.Comment.subject_type.in_(types), models.Comment.subject_id.in_(ids)).order_by(models.Comment.created_at, models.Comment.id)
                ) if (row.subject_type, row.subject_id) in subjects
            ]
        manifest = {
            "format": "titles-dam-export",
            "version": 2,
            "created_at": datetime.now().astimezone(),
            "selection": {
                "project_id": selection["selected_project_id"],
                "asset_ids": sorted(str(item) for item in payload.get("asset_ids", [])),
                "dataset_version_ids": sorted(str(item) for item in payload.get("dataset_version_ids", [])),
                "model_version_ids": sorted(str(item) for item in payload.get("model_version_ids", [])),
                "eval_run_ids": sorted(str(item) for item in payload.get("eval_run_ids", [])),
            },
            **rows,
        }
        return manifest, assets
