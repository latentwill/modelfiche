from __future__ import annotations

import json
import hashlib
import mimetypes
import re
import yaml
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import models
from ...checkpoint_revisions import establish_checkpoint_revision
from ...storage.repository import StorageRepository
from ...training_metrics import MetricInput, ingest_metric_batch
from .detection import PrefixKind
from .importer import ImportContext, ImportResult
from .metrics import parse_loss_log_db, parse_tensorboard_event, parse_training_log
from .models import ObjectInfo
from .reconciliation import StoredObject

_STEP = re.compile(r"(?:step|checkpoint|ckpt|iteration|iter)[-_]?(\d+)", re.IGNORECASE)

def _supported_fal_endpoint(base_model: str | None) -> str | None:
    normalized = (base_model or "").casefold()
    if "krea-2" in normalized or "krea 2" in normalized or "krea2" in normalized:
        return "fal-ai/krea-2/turbo/lora"
    if "ideogram" in normalized:
        return "ideogram/v4/lora"
    return None



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
            version = self._referenced_dataset_version(project_id, context.prefix, name)
        if version is None:
            dataset = self._dataset_for_import(project_id, context.prefix, name)
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
                version.name = dataset.name
            version.source_uri = source_uri
            version.status = "published"
            if context.requested_by_profile_id:
                version.published_by_profile_id = context.requested_by_profile_id
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
                # Dataset ownership and gallery categorization must agree. A dataset
                # discovered under another project's import job may be reassigned
                # from its path, so carry its member assets to that project too.
                asset.project_id = project_id
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

    def _dataset_for_import(self, project_id: str, prefix: str, name: str) -> models.Dataset | None:
        candidates = self._dataset_name_candidates(prefix, name)
        datasets = list(
            self.session.scalars(
                select(models.Dataset).where(
                    models.Dataset.project_id == project_id,
                    models.Dataset.name.in_(candidates),
                )
            )
        )
        if not datasets:
            return None
        return sorted(datasets, key=lambda dataset: candidates.index(dataset.name))[0]

    def _referenced_dataset_version(self, project_id: str, prefix: str, name: str) -> models.DatasetVersion | None:
        candidates = self._dataset_name_candidates(prefix, name)
        rows = self.session.execute(
            select(models.DatasetVersion, models.Dataset)
            .join(models.Dataset, models.Dataset.id == models.DatasetVersion.dataset_id)
            .where(
                models.Dataset.project_id == project_id,
                models.DatasetVersion.status == "referenced",
            )
            .order_by(models.DatasetVersion.version_number.desc())
        ).all()
        for version, dataset in rows:
            source_uri = str(version.source_uri or "")
            source_name = PurePosixPath(source_uri.removeprefix("training-path://")).name if source_uri.startswith("training-path://") else ""
            if dataset.name in candidates or source_name in candidates:
                return version
        return None

    def _dataset_name_candidates(self, prefix: str, name: str) -> list[str]:
        candidates = [name]
        parts = PurePosixPath(prefix.rstrip("/")).parts
        lowered = [part.lower() for part in parts]
        for marker in ("datasets", "dataset"):
            if marker not in lowered:
                continue
            index = lowered.index(marker)
            if index + 1 < len(parts):
                owner = parts[index + 1]
                if owner.lower() != name.lower():
                    candidates.append(f"{owner}-{name}")
            break
        return list(dict.fromkeys(candidates))

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
        source = self._source(context.source_id)
        manifest = result.detection.metadata.get("manifest", {})
        state = self._json_named("run_state.json")
        training_config = self._training_config()
        process = _training_process(training_config)
        model_config = process.get("model", {}) if isinstance(process.get("model"), dict) else {}
        train_config = process.get("train", {}) if isinstance(process.get("train"), dict) else {}
        config_name = _first(training_config.get("config", {}) if isinstance(training_config.get("config"), dict) else {}, "name")
        trainer = _first(manifest, "trainer", "training_tool") or ("ai-toolkit" if process else None)
        base_model = _first(manifest, "base_model", "model") or _first(model_config, "name_or_path", "model", "path")
        normalized = _normalize_config(manifest, state)
        manifest_dataset = _first(manifest, "dataset_id", "dataset", "dataset_path", "dataset_uri")
        if manifest_dataset:
            normalized["dataset_sources"] = [str(manifest_dataset)]
        if training_config:
            configured_datasets = [item.get("folder_path") for item in process.get("datasets", []) if isinstance(item, dict) and item.get("folder_path")]
            direct_dataset = _first(training_config, "dataset_base_path", "dataset_path", "dataset_uri")
            if not configured_datasets and direct_dataset:
                configured_datasets = [str(direct_dataset)]
            normalized = {
                **normalized,
                "trainer": trainer,
                "base_model": base_model,
                "steps": _first(train_config, "steps", "max_steps", "total_steps") or _first(training_config, "max_steps", "total_steps"),
                "epochs": _first(train_config, "epochs", "num_epochs") or _first(training_config, "epochs", "num_epochs"),
                "learning_rate": _first(train_config, "lr", "learning_rate") or _first(training_config, "lr", "learning_rate"),
                "lora_rank": _first(process.get("network", {}) if isinstance(process.get("network"), dict) else {}, "linear", "rank") or _first(training_config, "lora_rank", "network_dim"),
                "dataset_sources": configured_datasets or normalized.get("dataset_sources", []),
                "training_config": training_config,
            }
        status = _public_run_status(_first(state, "status", "state"))
        project_id = self._run_project_id(context, f"{context.prefix} {config_name or ''} {json.dumps(normalized, sort_keys=True)}")
        run = self.session.scalar(
            select(models.TrainingRun).where(
                models.TrainingRun.origin_source_id == source.id,
                models.TrainingRun.source_prefix == context.prefix,
            )
        )
        if run is None:
            run = models.TrainingRun(
                project_id=project_id,
                name=str(config_name or PurePosixPath(context.prefix.rstrip("/")).name),
                trainer=trainer,
                base_model=base_model,
                status=status,
                source_prefix=context.prefix,
                origin_source_id=source.id,
                origin_source_fingerprint=source.identity_fingerprint,
                raw_manifest=manifest,
                raw_state=state,
                normalized_config=normalized,
            )
            self.session.add(run)
            self.session.flush()
        else:
            run.project_id = project_id
            run.origin_source_id = source.id
            run.origin_source_fingerprint = source.identity_fingerprint
            run.raw_manifest = manifest
            run.raw_state = state
            run.name = str(config_name or run.name)
            run.trainer = trainer or run.trainer
            run.base_model = base_model or run.base_model
            run.normalized_config = normalized
            run.status = status
        for asset_id in self._assets.values():
            asset = self.session.get(models.Asset, asset_id)
            if asset is not None:
                asset.project_id = run.project_id
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
            establish_checkpoint_revision(self.session, checkpoint)
            checkpoints[identity] = checkpoint
        existing_sample_assets = set(self.session.scalars(select(models.Sample.asset_id).where(models.Sample.run_id == run.id)))
        for key, asset_id in self._assets.items():
            if asset_id in existing_sample_assets:
                continue
            asset = self.session.get(models.Asset, asset_id)
            if not asset or asset.metadata_.get("category") != "sample":
                continue
            asset.project_id = run.project_id
            asset.metadata_ = {**dict(asset.metadata_ or {}), "category": "sample"}
            step = _extract_step(PurePosixPath(key).name)
            stage_name = _stage_name(context.prefix, key)
            stage_id = stages[stage_name].id if stage_name else None
            checkpoint = checkpoints.get((stage_id, step)) if step is not None else None
            location = self.session.scalar(
                select(models.AssetLocation).where(models.AssetLocation.asset_id == asset_id).order_by(models.AssetLocation.modified_at.desc())
            )
            self.session.add(models.Sample(
                run_id=run.id, checkpoint_id=checkpoint.id if checkpoint else None,
                asset_id=asset_id, step=step,
                modified_at=location.modified_at if location else None,
            ))
        self._persist_metrics(run)
        if run.status in {"failed", "unknown"} and checkpoints:
            run.normalized_config = {
                **dict(run.normalized_config or {}),
                "source_status": run.status,
                "source_message": _first(state, "message", "error"),
                "usable_output": True,
                "status_evidence": f"{len(checkpoints)} checkpoint(s) imported",
            }
            run.status = "completed"
        self._link_dataset(run)
        self._reconcile_model(run, checkpoints.values())
        return run.id

    def _training_config(self) -> dict[str, Any]:
        for key, body in self._snapshots.items():
            if PurePosixPath(key).name.lower() not in {"config.yaml", "config.yml", "train.yaml", "train.yml", "config.json", "train_config.json", "training_args.json"}:
                continue
            try:
                value = yaml.safe_load(body)
                if isinstance(value, dict):
                    return value
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
        return {}

    def _link_dataset(self, run: models.TrainingRun) -> None:
        if run.dataset_version_id:
            linked_version = self.session.get(models.DatasetVersion, run.dataset_version_id)
            linked_dataset = self.session.get(models.Dataset, linked_version.dataset_id) if linked_version else None
            if linked_dataset and linked_dataset.project_id == run.project_id:
                return
            run.dataset_version_id = None
        sources = run.normalized_config.get("dataset_sources", []) if isinstance(run.normalized_config, dict) else []
        source_names = [PurePosixPath(str(source)).name for source in sources if source]
        if sources:
            source_path = str(sources[0])
            source_name = PurePosixPath(source_path).name
            resolved_name = f"{run.name}-dataset" if source_name.lower() in {"dataset", "datasets", "data"} else source_name
            project_versions = self.session.execute(select(models.DatasetVersion, models.Dataset).join(models.Dataset, models.Dataset.id == models.DatasetVersion.dataset_id).where(models.Dataset.project_id == run.project_id).order_by(models.DatasetVersion.version_number.desc())).all()
            matches = [(version, dataset) for version, dataset in project_versions if dataset.name == resolved_name or resolved_name.lower().endswith("-" + dataset.name.lower())]
            if len({dataset.id for _version, dataset in matches}) == 1:
                run.dataset_version_id = matches[0][0].id
                return
        if not sources:
            return
        source_path = str(sources[0])
        source_name = PurePosixPath(source_path).name
        if source_name.lower() in {"dataset", "datasets", "data"}:
            source_name = f"{run.name}-dataset"
        dataset = self.session.scalar(select(models.Dataset).where(models.Dataset.project_id == run.project_id, models.Dataset.name == source_name))
        if dataset is None:
            dataset = models.Dataset(project_id=run.project_id, name=source_name, description=f"Training dataset referenced by {run.name}: {source_path}")
            self.session.add(dataset)
            self.session.flush()
        version = self.session.scalar(select(models.DatasetVersion).where(models.DatasetVersion.dataset_id == dataset.id, models.DatasetVersion.source_uri == f"training-path://{source_path}"))
        if version is None:
            next_number = int(self.session.scalar(select(func.max(models.DatasetVersion.version_number)).where(models.DatasetVersion.dataset_id == dataset.id)) or 0) + 1
            version = models.DatasetVersion(dataset_id=dataset.id, version_number=next_number, name=source_name, source_uri=f"training-path://{source_path}", caption_format="text", status="referenced", published_by_profile_id=None)
            self.session.add(version)
            self.session.flush()
        run.dataset_version_id = version.id

    def _run_project_id(self, context: ImportContext, evidence: str) -> str:
        requested_project = self.session.get(models.Project, context.project_id)
        if requested_project is None:
            return context.project_id
        matches = []
        for project in self.session.scalars(
            select(models.Project).where(
                models.Project.workspace_id == requested_project.workspace_id,
                models.Project.state != "archived",
            )
        ):
            triggers = [str(trigger).strip().casefold() for trigger in project.trigger_words if str(trigger).strip()]
            if any(re.search(rf"(?<![a-z0-9]){re.escape(trigger)}(?![a-z0-9])", evidence.casefold()) for trigger in triggers):
                matches.append(project.id)
        return matches[0] if len(matches) == 1 else context.project_id

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
                version.model_id = model.id
                readiness = {**dict(version.readiness or {}), "imported": True, "training_run_id": run.id}
                endpoint = _supported_fal_endpoint(version.base_model or run.base_model or model.name)
                if readiness.get("fal_url") and not any(readiness.get(key) for key in ("endpoint_id", "endpoint", "fal_endpoint")) and endpoint:
                    readiness["endpoint_id"] = endpoint
                version.readiness = readiness
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
            name = PurePosixPath(key).name.lower()
            parser = parse_tensorboard_event if name.startswith("events.out.tfevents.") else (parse_loss_log_db if PurePosixPath(key).suffix.lower() == ".db" else parse_training_log)
            parsed = parser(body)
            if not parsed:
                continue
            points = [
                MetricInput(
                    step=point.step,
                    name=point.name,
                    value=point.value,
                    wall_time=point.wall_time,
                    source_key=key,
                )
                for point in parsed
            ]
            batch_digest = hashlib.sha256()
            batch_digest.update(key.encode())
            batch_digest.update(b"\0")
            batch_digest.update(body)
            ingest_metric_batch(
                self.session,
                run.id,
                points,
                batch_key=f"s3:{batch_digest.hexdigest()}",
                committed_step=max(point.step for point in points),
            )

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
    timestamp_sample = re.search(r"__(\d{6,})_\d+(?:\.[^.]+)?$", name)
    if timestamp_sample:
        return int(timestamp_sample.group(1))
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
    ignored = {"titlesxyz", "title", "captions", "caption", "image", "images", "run", "dataset", "vcribb", "kzapata"}
    return {
        token for token in re.split(r"[^a-z0-9]+", value.lower())
        if token and token not in ignored and not re.fullmatch(r"v?\d+", token)
    }


def _first(value: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if value.get(key) is not None:
            return value[key]
    return None


def _training_process(config: dict[str, Any]) -> dict[str, Any]:
    root = config.get("config") if isinstance(config.get("config"), dict) else config
    processes = root.get("process", []) if isinstance(root, dict) else []
    return processes[0] if isinstance(processes, list) and processes and isinstance(processes[0], dict) else {}


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
    name = PurePosixPath(key).name.lower()
    return name in {"aitk_db.db", "loss_log.db", "log.txt", "train.log", "training.log"} or name.startswith("events.out.tfevents.")



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
