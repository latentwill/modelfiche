from __future__ import annotations

import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import models
from ...storage.repository import StorageRepository
from .detection import PrefixKind
from .importer import ImportContext, ImportResult
from .metrics import parse_loss_log_db, parse_training_log
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
            changed = location.etag != item.etag or location.size != item.size
            location.workspace_id = source.workspace_id
            location.source_id = source.id
            location.source_revision_fingerprint = source.identity_fingerprint
            location.uri = f"s3://{source.id}/{item.key}"
            location.etag = item.etag
            location.size = item.size
            location.modified_at = item.modified_at
            location.last_seen_at = datetime.now(timezone.utc)
            location.hydration_state = "remote"
            if changed:
                location.verified_size = None
                location.verified_sha256 = None
                location.verification_state = "unverified"
                location.last_verified_at = None
            asset = location.asset
            asset.metadata_ = {**asset.metadata_, "category": category}
        else:
            asset = models.Asset(
                workspace_id=source.workspace_id,
                kind=_asset_kind(category),
                name=PurePosixPath(item.key).name,
                mime_type=mimetypes.guess_type(item.key)[0],
                metadata_={"category": category},
            )
            self.session.add(asset)
            self.session.flush()
            location = models.AssetLocation(
                asset_id=asset.id,
                workspace_id=source.workspace_id,
                provider="s3",
                uri=f"s3://{source.id}/{item.key}",
                bucket=source.bucket,
                object_key=item.key,
                etag=item.etag,
                source_id=source.id,
                source_revision_fingerprint=source.identity_fingerprint,
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
        if asset_id and _is_text_snapshot(key):
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
        self._verify_remote_assets(context)
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
        version = self.session.scalar(select(models.DatasetVersion).where(models.DatasetVersion.source_uri == source_uri))
        name = PurePosixPath(context.prefix.rstrip("/")).name
        project_id = self._dataset_project_id(context)
        if version is None:
            dataset = self.session.scalar(
                select(models.Dataset).where(models.Dataset.project_id == project_id, models.Dataset.name == name)
            )
            if dataset is None:
                dataset = models.Dataset(project_id=project_id, name=name)
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
        else:
            dataset = self.session.get(models.Dataset, version.dataset_id)
            if dataset is not None:
                dataset.project_id = project_id
        captions = self._captions(context.prefix)
        image_keys = [key for key in self._assets if PurePosixPath(key).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".tif", ".tiff"}]
        existing_items = {
            item.asset_id: item
            for item in self.session.scalars(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == version.id))
        }
        for position, key in enumerate(sorted(image_keys)):
            relative = key[len(context.prefix.rstrip("/") + "/") :]
            stem = str(PurePosixPath(relative).with_suffix(""))
            caption = captions.get(relative, captions.get(stem, captions.get(PurePosixPath(relative).name, "")))
            asset_id = self._assets[key]
            asset = self.session.get(models.Asset, asset_id)
            if asset is not None:
                asset.metadata_ = {**dict(asset.metadata_ or {}), "category": "dataset_image", "caption": caption, "dataset_version_id": version.id}
            item = existing_items.get(asset_id)
            if item is None:
                item = models.DatasetItem(
                    dataset_version_id=version.id,
                    asset_id=asset_id,
                    caption=caption,
                    caption_format="text",
                    included=True,
                    position=position,
                )
                self.session.add(item)
            else:
                item.position = position
                if caption:
                    item.caption = caption
        return version.id

    def _dataset_project_id(self, context: ImportContext) -> str:
        parts = PurePosixPath(context.prefix.rstrip("/")).parts
        for marker in ("datasets", "dataset"):
            if marker not in {part.lower() for part in parts}:
                continue
            index = next(i for i, part in enumerate(parts) if part.lower() == marker)
            if index + 1 >= len(parts):
                break
            owner = parts[index + 1]
            project = self.session.scalar(
                select(models.Project).where(
                    func.lower(models.Project.title) == owner.lower(),
                    models.Project.state != "archived",
                )
            )
            if project is not None:
                return project.id
        return context.project_id

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
        source = self._source(context.source_id)
        manifest = result.detection.metadata.get("manifest", {})
        state = self._json_named("run_state.json")
        status = _public_run_status(_first(state, "status", "state"))
        if run is None:
            run = models.TrainingRun(
                project_id=context.project_id,
                name=PurePosixPath(context.prefix.rstrip("/")).name,
                trainer=_first(manifest, "trainer", "training_tool"),
                base_model=_first(manifest, "base_model", "model"),
                status=status,
                source_prefix=context.prefix,
                origin_source_id=source.id,
                origin_source_fingerprint=source.identity_fingerprint,
                raw_manifest=manifest,
                raw_state=state,
                normalized_config=_normalize_config(manifest, state),
            )
            self.session.add(run)
            self.session.flush()
        else:
            run.origin_source_id = source.id
            run.origin_source_fingerprint = source.identity_fingerprint
            run.raw_manifest = manifest
            run.raw_state = state
            run.normalized_config = _normalize_config(manifest, state)
            run.status = status
        stage_names = {
            name
            for key in self._assets
            if (name := _stage_name(context.prefix, key)) is not None
        }
        stages: dict[str, models.TrainingStage] = {
            item.name: item
            for item in self.session.scalars(select(models.TrainingStage).where(models.TrainingStage.run_id == run.id))
        }
        for name, stage in list(stages.items()):
            if _is_internal_stage(name):
                self.session.delete(stage)
                stages.pop(name)
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
        self._persist_metrics(run)
        self._link_dataset(run)
        self._reconcile_model(run, checkpoints.values())
        return run.id

    def _link_dataset(self, run: models.TrainingRun) -> None:
        if run.dataset_version_id:
            return
        run_tokens = _identity_tokens(run.name)
        candidates = self.session.execute(
            select(models.DatasetVersion, models.Dataset)
            .join(models.Dataset, models.Dataset.id == models.DatasetVersion.dataset_id)
            .where(models.Dataset.project_id == run.project_id)
        ).all()
        ranked = sorted(
            ((len(run_tokens & _identity_tokens(dataset.name)), version) for version, dataset in candidates),
            key=lambda item: item[0],
            reverse=True,
        )
        if ranked and ranked[0][0] >= 1 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
            run.dataset_version_id = ranked[0][1].id

    def _reconcile_model(self, run: models.TrainingRun, checkpoints) -> None:
        marker = f"Imported from training run {run.id}"
        model = self.session.scalar(
            select(models.Model).where(
                models.Model.project_id == run.project_id,
                models.Model.description == marker,
            )
        )
        if model is None:
            model = self.session.scalar(
                select(models.Model).where(
                    models.Model.project_id == run.project_id,
                    models.Model.name == run.name,
                ).order_by(models.Model.created_at.asc())
            )
        if model is None:
            model = models.Model(project_id=run.project_id, name=run.name, description=marker)
            self.session.add(model)
            self.session.flush()
        else:
            model.name = run.name
            model.description = marker
        for checkpoint in checkpoints:
            version = self.session.scalar(
                select(models.ModelVersion).where(models.ModelVersion.checkpoint_id == checkpoint.id)
            )
            if version is not None:
                continue
            asset = self.session.get(models.Asset, checkpoint.asset_id)
            version_name = PurePosixPath(asset.name).stem if asset else f"step-{checkpoint.step}"
            self.session.add(models.ModelVersion(
                model_id=model.id,
                checkpoint_id=checkpoint.id,
                name=version_name,
                base_model=run.base_model,
                lifecycle_state="candidate",
                readiness={"imported": True, "training_run_id": run.id},
            ))

    def _persist_metrics(self, run: models.TrainingRun) -> None:
        for key, body in self._snapshots.items():
            if not _is_metric_source(key):
                continue
            parser = parse_loss_log_db if PurePosixPath(key).suffix.lower() == ".db" else parse_training_log
            for point in parser(body):
                metric = self.session.scalar(
                    select(models.TrainingMetric).where(
                        models.TrainingMetric.run_id == run.id,
                        models.TrainingMetric.step == point.step,
                        models.TrainingMetric.name == point.name,
                    )
                )
                if metric is None:
                    metric = models.TrainingMetric(
                        run_id=run.id,
                        step=point.step,
                        name=point.name,
                        value=point.value,
                        wall_time=point.wall_time,
                        source_key=key,
                    )
                    self.session.add(metric)
                else:
                    metric.value = point.value
                    metric.wall_time = point.wall_time
                    metric.source_key = key

    def _verify_remote_assets(self, context: ImportContext) -> None:
        source = self._source(context.source_id)
        known_keys = set(self._assets)
        checksums: dict[str, str] = {}
        for key, body in self._snapshots.items():
            if PurePosixPath(key).name.lower() != "checksums.sha256":
                continue
            checksums.update(_parse_checksums(key, body, context.prefix, known_keys))
        if not checksums:
            return
        repository = StorageRepository(self.session, source.workspace_id)
        for key, asset_id in self._assets.items():
            digest = checksums.get(key)
            if digest is None:
                continue
            location = self.session.scalar(
                select(models.AssetLocation).where(
                    models.AssetLocation.asset_id == asset_id,
                    models.AssetLocation.source_id == source.id,
                    models.AssetLocation.object_key == key,
                )
            )
            if location is None or location.size is None:
                continue
            repository.attach_verified_remote_location(
                asset_id=asset_id,
                source_id=source.id,
                object_key=key,
                etag=location.etag,
                size=location.size,
                sha256=digest,
                mime_type=self.session.get(models.Asset, asset_id).mime_type or "application/octet-stream",
                modified_at=location.modified_at,
            )

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
    if first.lower() in {"checkpoints", "checkpoint", "samples", "sample", "logs", "config", "configs", "output", "outputs"} or _is_internal_stage(first):
        return None
    return first


def _is_internal_stage(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return "backup" in normalized.split("_") or normalized == "monitoring"


def _public_run_status(value: Any) -> str:
    status = str(value or "unknown")
    return "completed" if _is_internal_stage(status) else status


def _identity_tokens(value: str) -> set[str]:
    ignored = {"titlesxyz", "title", "captions", "caption", "image", "images", "run", "dataset"}
    return {
        token for token in re.split(r"[^a-z0-9]+", value.lower())
        if token and token not in ignored and not re.fullmatch(r"v?\d+", token)
    }


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
            "status": _public_run_status(_first(state, "status", "state")),
        }.items()
        if value is not None
    }

def _is_metric_source(key: str) -> bool:
    return PurePosixPath(key).name.lower() in {"aitk_db.db", "loss_log.db", "train.log", "training.log"}



def _is_text_snapshot(key: str) -> bool:
    name = PurePosixPath(key).name.lower()
    return PurePosixPath(name).suffix in {".json", ".jsonl", ".log", ".out", ".txt", ".yaml", ".yml", ".sha256"}

def _parse_checksums(key: str, body: bytes, prefix: str, known_keys: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    parent = key.rsplit("/", 1)[0] if "/" in key else prefix.rstrip("/")
    for line in body.decode("utf-8", "replace").splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            continue
        relative = fields[1].lstrip("*").strip().lstrip("./")
        candidates = (
            relative,
            f"{prefix.rstrip('/')}/{relative}",
            f"{parent}/{relative}",
        )
        resolved = next((candidate for candidate in candidates if candidate in known_keys), None)
        if resolved is not None:
            result[resolved] = fields[0].lower()
    return result
