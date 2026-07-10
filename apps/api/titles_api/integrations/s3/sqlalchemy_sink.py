from __future__ import annotations

import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from .detection import PrefixKind
from .importer import ImportContext, ImportResult
from .models import ObjectInfo
from .reconciliation import StoredObject

_STEP = re.compile(r"(?:step|checkpoint|ckpt|iteration|iter)[-_]?(\d+)", re.IGNORECASE)


class SQLAlchemyImportSink:
    def __init__(self, session: Session):
        self.session = session
        self._assets: dict[str, str] = {}
        self._snapshots: dict[str, bytes] = {}

    def stored_objects(self, source_id: str, prefix: str) -> list[StoredObject]:
        source = self._source(source_id)
        rows = self.session.scalars(
            select(models.AssetLocation).where(
                models.AssetLocation.provider == "s3",
                models.AssetLocation.bucket == source.bucket,
                models.AssetLocation.object_key.startswith(prefix),
            )
        )
        return [StoredObject(row.object_key or "", row.etag or "", row.size or 0) for row in rows]

    def upsert_remote_object(self, source_id: str, item: ObjectInfo, *, category: str) -> str:
        source = self._source(source_id)
        location = self.session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.provider == "s3",
                models.AssetLocation.bucket == source.bucket,
                models.AssetLocation.object_key == item.key,
            )
        )
        if location:
            location.etag = item.etag
            location.size = item.size
            location.modified_at = item.modified_at
            location.last_seen_at = datetime.now(timezone.utc)
            location.hydration_state = "remote"
            asset = location.asset
            asset.metadata_ = {**asset.metadata_, "category": category}
        else:
            workspace_id = source.workspace_id
            asset = models.Asset(
                workspace_id=workspace_id,
                kind=_asset_kind(category),
                name=PurePosixPath(item.key).name,
                mime_type=mimetypes.guess_type(item.key)[0],
                metadata_={"category": category},
            )
            self.session.add(asset)
            self.session.flush()
            location = models.AssetLocation(
                asset_id=asset.id,
                provider="s3",
                uri=f"s3://{source.bucket}/{item.key}",
                bucket=source.bucket,
                object_key=item.key,
                etag=item.etag,
                size=item.size,
                modified_at=item.modified_at,
                hydration_state="remote",
            )
            self.session.add(location)
        self._assets[item.key] = asset.id
        return asset.id

    def mark_remote_missing(self, source_id: str, key: str) -> None:
        source = self._source(source_id)
        location = self.session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.provider == "s3",
                models.AssetLocation.bucket == source.bucket,
                models.AssetLocation.object_key == key,
            )
        )
        if location:
            location.hydration_state = "remote_missing"

    def save_source_snapshot(self, source_id: str, key: str, body: bytes) -> None:
        self._snapshots[key] = body
        asset_id = self._assets.get(key)
        if asset_id:
            asset = self.session.get(models.Asset, asset_id)
            if asset:
                asset.metadata_ = {**asset.metadata_, "source_text": body.decode("utf-8", "replace")}

    def finalize_import(self, context: ImportContext, result: ImportResult) -> None:
        for asset_id in self._assets.values():
            asset = self.session.get(models.Asset, asset_id)
            if asset and asset.project_id is None:
                asset.project_id = context.project_id
        if result.detection.kind == PrefixKind.DATASET:
            subject_id = self._finalize_dataset(context)
            subject_type = "dataset_version"
        else:
            subject_id = self._finalize_run(context, result)
            subject_type = "training_run"
        import_job = self.session.scalar(
            select(models.ImportJob).where(
                models.ImportJob.source_id == context.source_id,
                models.ImportJob.project_id == context.project_id,
                models.ImportJob.prefix == context.prefix,
            ).order_by(models.ImportJob.created_at.desc())
        )
        if import_job:
            import_job.detected_type = result.detection.kind.value
            import_job.state = "succeeded"
            import_job.progress = 1
            import_job.warnings = [{"message": message} for message in result.warnings]
            import_job.result = result.as_json()
        source = self._source(context.source_id)
        self.session.add(
            models.ActivityEvent(
                workspace_id=source.workspace_id,
                project_id=context.project_id,
                profile_id=context.requested_by_profile_id,
                action="import.completed",
                subject_type=subject_type,
                subject_id=subject_id,
                details={"prefix": context.prefix, "kind": result.detection.kind.value, "indexed": result.indexed},
            )
        )
        self.session.commit()

    def _finalize_dataset(self, context: ImportContext) -> str:
        source_uri = f"s3://{self._source(context.source_id).bucket}/{context.prefix.rstrip('/')}"
        existing = self.session.scalar(select(models.DatasetVersion).where(models.DatasetVersion.source_uri == source_uri))
        if existing:
            return existing.id
        name = PurePosixPath(context.prefix.rstrip("/")).name
        dataset = self.session.scalar(
            select(models.Dataset).where(models.Dataset.project_id == context.project_id, models.Dataset.name == name)
        )
        if dataset is None:
            dataset = models.Dataset(project_id=context.project_id, name=name)
            self.session.add(dataset)
            self.session.flush()
        max_version = max((v.version_number for v in self.session.scalars(select(models.DatasetVersion).where(models.DatasetVersion.dataset_id == dataset.id))), default=0)
        version = models.DatasetVersion(
            dataset_id=dataset.id,
            version_number=max_version + 1,
            name=name,
            source_uri=source_uri,
            caption_format="text",
            status="published",
            published_by_profile_id=context.requested_by_profile_id,
        )
        self.session.add(version)
        self.session.flush()
        captions = self._captions(context.prefix)
        image_keys = [key for key in self._assets if PurePosixPath(key).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".tif", ".tiff"}]
        for position, key in enumerate(sorted(image_keys)):
            relative = key[len(context.prefix.rstrip("/") + "/") :]
            stem = str(PurePosixPath(relative).with_suffix(""))
            self.session.add(
                models.DatasetItem(
                    dataset_version_id=version.id,
                    asset_id=self._assets[key],
                    caption=captions.get(relative, captions.get(stem, captions.get(PurePosixPath(relative).name, ""))),
                    caption_format="text",
                    included=True,
                    position=position,
                )
            )
        return version.id

    def _captions(self, prefix: str) -> dict[str, str]:
        captions: dict[str, str] = {}
        for key, body in self._snapshots.items():
            relative = key[len(prefix.rstrip("/") + "/") :]
            path = PurePosixPath(relative)
            if path.name.lower() == "captions.jsonl":
                for line in body.decode("utf-8", "replace").splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    filename = row.get("image") or row.get("file") or row.get("path") or row.get("filename")
                    caption = row.get("caption") or row.get("text") or row.get("prompt")
                    if filename and caption is not None:
                        captions[str(filename)] = str(caption)
                        captions[str(PurePosixPath(str(filename)).with_suffix(""))] = str(caption)
            elif path.suffix.lower() == ".txt":
                captions[str(path.with_suffix(""))] = body.decode("utf-8", "replace").strip()
        return captions

    def _finalize_run(self, context: ImportContext, result: ImportResult) -> str:
        run = self.session.scalar(
            select(models.TrainingRun).where(
                models.TrainingRun.project_id == context.project_id,
                models.TrainingRun.source_prefix == context.prefix,
            )
        )
        manifest = result.detection.metadata.get("manifest", {})
        state = self._json_named("run_state.json")
        if run is None:
            run = models.TrainingRun(
                project_id=context.project_id,
                name=PurePosixPath(context.prefix.rstrip("/")).name,
                trainer=_first(manifest, "trainer", "training_tool"),
                base_model=_first(manifest, "base_model", "model"),
                status=str(_first(state, "status", "state") or "unknown"),
                source_prefix=context.prefix,
                raw_manifest=manifest,
                raw_state=state,
                normalized_config=_normalize_config(manifest, state),
            )
            self.session.add(run)
            self.session.flush()
        else:
            run.raw_manifest = manifest
            run.raw_state = state
            run.normalized_config = _normalize_config(manifest, state)
        stage_names = {
            name
            for key in self._assets
            if (name := _stage_name(context.prefix, key)) is not None
        }
        stages: dict[str, models.TrainingStage] = {
            item.name: item
            for item in self.session.scalars(select(models.TrainingStage).where(models.TrainingStage.run_id == run.id))
        }
        for name in sorted(stage_names):
            if name not in stages:
                stage = models.TrainingStage(run_id=run.id, name=name, config=self._stage_config(context.prefix, name))
                self.session.add(stage)
                self.session.flush()
                stages[name] = stage
        checkpoints: dict[tuple[str | None, int], models.Checkpoint] = {
            (item.stage_id, item.step): item
            for item in self.session.scalars(select(models.Checkpoint).where(models.Checkpoint.run_id == run.id))
        }
        for key, asset_id in self._assets.items():
            if PurePosixPath(key).suffix.lower() != ".safetensors":
                continue
            step = _extract_step(PurePosixPath(key).name)
            stage_name = _stage_name(context.prefix, key)
            stage_id = stages[stage_name].id if stage_name else None
            identity = (stage_id, step) if step is not None else None
            if identity is None or identity in checkpoints:
                continue
            checkpoint = models.Checkpoint(run_id=run.id, stage_id=stage_id, step=step, asset_id=asset_id, state="available")
            self.session.add(checkpoint)
            self.session.flush()
            checkpoints[identity] = checkpoint
        existing_sample_assets = set(self.session.scalars(select(models.Sample.asset_id).where(models.Sample.run_id == run.id)))
        for key, asset_id in self._assets.items():
            if asset_id in existing_sample_assets:
                continue
            asset = self.session.get(models.Asset, asset_id)
            if not asset or asset.metadata_.get("category") != "sample":
                continue
            step = _extract_step(PurePosixPath(key).name)
            stage_name = _stage_name(context.prefix, key)
            stage_id = stages[stage_name].id if stage_name else None
            checkpoint = checkpoints.get((stage_id, step)) if step is not None else None
            self.session.add(models.Sample(run_id=run.id, checkpoint_id=checkpoint.id if checkpoint else None, asset_id=asset_id, step=step))
        return run.id

    def _stage_config(self, prefix: str, stage_name: str) -> dict[str, Any]:
        for key, body in self._snapshots.items():
            if _stage_name(prefix, key) != stage_name:
                continue
            if PurePosixPath(key).name.lower() not in {"config.json", "config.yaml", "config.yml", "train.yaml", "train.yml", "train_config.json"}:
                continue
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
            return {"source_key": key}
        return {}

    def _json_named(self, name: str) -> dict[str, Any]:
        for key, body in self._snapshots.items():
            if PurePosixPath(key).name.lower() == name:
                try:
                    value = json.loads(body)
                    return value if isinstance(value, dict) else {}
                except json.JSONDecodeError:
                    return {}
        return {}

    def _source(self, source_id: str) -> models.ImportSource:
        source = self.session.get(models.ImportSource, source_id)
        if not source:
            raise LookupError(f"import source not found: {source_id}")
        return source


def _asset_kind(category: str) -> models.AssetKind:
    return {
        "dataset_image": models.AssetKind.image,
        "sample": models.AssetKind.image,
        "checkpoint": models.AssetKind.model,
        "run_metadata": models.AssetKind.manifest,
        "source_metadata": models.AssetKind.config,
        "caption": models.AssetKind.caption,
        "log": models.AssetKind.log,
    }.get(category, models.AssetKind.other)


def _extract_step(name: str) -> int | None:
    match = _STEP.search(name)
    if match:
        return int(match.group(1))
    numbers = re.findall(r"\d+", PurePosixPath(name).stem)
    return int(numbers[-1]) if numbers else None


def _stage_name(prefix: str, key: str) -> str | None:
    root = prefix.rstrip("/") + "/"
    relative = key[len(root) :] if key.startswith(root) else key
    parts = PurePosixPath(relative).parts
    if len(parts) < 2:
        return None
    first = parts[0]
    if first.lower() in {"checkpoints", "checkpoint", "samples", "sample", "logs", "config", "configs", "output", "outputs"}:
        return None
    return first


def _first(value: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if value.get(key) is not None:
            return value[key]
    return None


def _normalize_config(manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "trainer": _first(manifest, "trainer", "training_tool"),
            "base_model": _first(manifest, "base_model", "model"),
            "steps": _first(manifest, "steps", "max_steps", "total_steps"),
            "status": _first(state, "status", "state"),
        }.items()
        if value is not None
    }
