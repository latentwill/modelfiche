from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from typing import Annotated, Any
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func, select
from .. import models, schemas
from ..checkpoint_revisions import establish_checkpoint_revision
from ..database import get_db
from ..services import active_profile, current_workspace, record_activity, workspace_object
from ..settings import get_settings

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_file(entry: dict[str, Any]) -> Path:
    source = Path(str(entry.get("source_path") or "")).expanduser().resolve()
    if not source.is_file():
        raise HTTPException(status_code=422, detail=f"bundle source does not exist: {source}")
    expected_size = int(entry.get("size") or -1)
    if source.stat().st_size != expected_size:
        raise HTTPException(status_code=422, detail=f"bundle source size changed: {source}")
    expected_sha = str(entry.get("sha256") or "").lower()
    if len(expected_sha) != 64 or _file_digest(source) != expected_sha:
        raise HTTPException(status_code=422, detail=f"bundle source checksum changed: {source}")
    return source


def _managed_copy(source: Path, entry: dict[str, Any], manifest_sha: str, model_key: str) -> Path:
    root = get_settings().asset_root.resolve()
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", model_key).strip("-") or "embedding"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-") or str(entry["sha256"])
    kind = "artifact" if entry.get("kind") == "embedding" else "images"
    destination = root / "embedding-bundles" / manifest_sha / safe_model / kind / f"{str(entry['sha256'])[:12]}-{safe_name}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if destination.stat().st_size != int(entry["size"]) or _file_digest(destination) != str(entry["sha256"]):
            raise HTTPException(status_code=409, detail=f"managed embedding content conflicts with {destination}")
        return destination
    temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
    try:
        shutil.copyfile(source, temporary)
        if temporary.stat().st_size != int(entry["size"]) or _file_digest(temporary) != str(entry["sha256"]):
            raise HTTPException(status_code=422, detail=f"managed copy verification failed: {source}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _archive_managed_copy(source: Path, entry: dict[str, Any], manifest_sha: str) -> Path:
    root = get_settings().asset_root.resolve()
    digest = str(entry["sha256"])
    destination = root / "embedding-archives" / manifest_sha / "objects" / digest[:2] / digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if destination.stat().st_size != int(entry["size"]) or _file_digest(destination) != digest:
            raise HTTPException(status_code=409, detail=f"managed embedding archive content conflicts with {destination}")
        return destination
    temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
    try:
        shutil.copyfile(source, temporary)
        if temporary.stat().st_size != int(entry["size"]) or _file_digest(temporary) != digest:
            raise HTTPException(status_code=422, detail=f"managed archive copy verification failed: {source}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _add_locations(
    db: Session,
    *,
    workspace_id: str,
    asset: models.Asset,
    source: Path,
    entry: dict[str, Any],
    bucket: str,
) -> None:
    modified_at = datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc)
    local = models.AssetLocation(
        asset_id=asset.id,
        workspace_id=workspace_id,
        provider="local",
        uri=source.as_uri(),
        relative_path=str(source),
        size=source.stat().st_size,
        verified_size=source.stat().st_size,
        verified_sha256=asset.sha256,
        verification_state="verified",
        hydration_state="hydrated",
        modified_at=modified_at,
        last_verified_at=datetime.now(timezone.utc),
    )
    db.add(local)
    db.flush()
    asset.preferred_location_id = local.id
    asset.origin_location_id = local.id
    object_key = str(entry.get("object_key") or "")
    if bucket and object_key:
        db.add(models.AssetLocation(
            asset_id=asset.id,
            workspace_id=workspace_id,
            provider="s3",
            uri=f"s3://{bucket}/{object_key}",
            bucket=bucket,
            object_key=object_key,
            size=source.stat().st_size,
            verified_size=source.stat().st_size,
            verified_sha256=asset.sha256,
            verification_state="manifest_verified",
            hydration_state="remote",
            modified_at=modified_at,
        ))


def _ensure_existing_managed_locations(
    db: Session,
    *,
    workspace_id: str,
    version: models.ModelVersion,
    manifest_sha: str,
    model_key: str,
    entries: list[tuple[dict[str, Any], Path]],
) -> int:
    checkpoint = db.get(models.Checkpoint, version.checkpoint_id)
    if checkpoint is None:
        raise HTTPException(status_code=409, detail=f"imported embedding checkpoint is missing for {model_key}")
    sample_asset_ids = list(db.scalars(select(models.Sample.asset_id).where(models.Sample.checkpoint_id == checkpoint.id)))
    assets = list(db.scalars(select(models.Asset).where(models.Asset.id.in_([checkpoint.asset_id, *sample_asset_ids]))))
    used_assets: set[str] = set()
    repaired = 0
    for entry, recovered_source in entries:
        asset = next(
            (
                candidate
                for candidate in assets
                if candidate.id not in used_assets
                and candidate.name == recovered_source.name
                and candidate.sha256 == str(entry["sha256"])
            ),
            None,
        )
        if asset is None:
            raise HTTPException(status_code=409, detail=f"imported embedding asset is missing for {recovered_source.name}")
        used_assets.add(asset.id)
        managed = _managed_copy(recovered_source, entry, manifest_sha, model_key)
        asset.metadata_ = {**dict(asset.metadata_ or {}), "recovered_source_path": str(recovered_source)}
        location = db.scalar(select(models.AssetLocation).where(
            models.AssetLocation.asset_id == asset.id,
            models.AssetLocation.provider == "local",
            models.AssetLocation.uri == managed.as_uri(),
        ))
        if location is None:
            modified_at = datetime.fromtimestamp(recovered_source.stat().st_mtime, tz=timezone.utc)
            location = models.AssetLocation(
                asset_id=asset.id,
                workspace_id=workspace_id,
                provider="local",
                uri=managed.as_uri(),
                relative_path=str(managed),
                size=managed.stat().st_size,
                verified_size=managed.stat().st_size,
                verified_sha256=asset.sha256,
                verification_state="verified",
                hydration_state="hydrated",
                modified_at=modified_at,
                last_verified_at=datetime.now(timezone.utc),
                repair_attribution="embedding_bundle.managed_copy",
            )
            db.add(location)
            db.flush()
            repaired += 1
        asset.preferred_location_id = location.id
        if asset.origin_location_id is None:
            asset.origin_location_id = location.id
    return repaired


def _existing_import(db: Session, project_id: str, import_key: str) -> models.ModelVersion | None:
    versions = db.scalars(
        select(models.ModelVersion)
        .join(models.Model, models.Model.id == models.ModelVersion.model_id)
        .where(models.Model.project_id == project_id)
    ).all()
    return next(
        (
            version
            for version in versions
            if isinstance(version.compatibility, dict)
            and isinstance(version.compatibility.get("import"), dict)
            and version.compatibility["import"].get("key") == import_key
        ),
        None,
    )


def _existing_archive_run(db: Session, project_id: str, import_key: str) -> models.TrainingRun | None:
    runs = db.scalars(select(models.TrainingRun).where(models.TrainingRun.project_id == project_id)).all()
    return next(
        (
            run
            for run in runs
            if isinstance(run.raw_manifest, dict)
            and run.raw_manifest.get("archive_import_key") == import_key
        ),
        None,
    )


def _archive_asset_kind(value: str) -> models.AssetKind:
    return {
        "embedding": models.AssetKind.model,
        "image": models.AssetKind.image,
        "config": models.AssetKind.config,
        "manifest": models.AssetKind.manifest,
        "log": models.AssetKind.log,
    }.get(value, models.AssetKind.other)


def _create_archive_asset(
    db: Session,
    *,
    workspace_id: str,
    project_id: str,
    run: models.TrainingRun | None,
    entry: dict[str, Any],
    source: Path,
    managed: Path,
    manifest: dict[str, Any],
    manifest_sha: str,
    family_key: str | None,
    run_key: str | None,
    extra_metadata: dict[str, Any] | None = None,
) -> models.Asset:
    bucket = str(manifest.get("bucket") or "")
    metadata = {
        **dict(entry.get("metadata") or {}),
        "category": str(entry.get("kind") or "evidence"),
        "artifact_type": "embedding" if entry.get("kind") == "embedding" else None,
        "archive_family_key": family_key,
        "archive_run_key": run_key,
        "run_id": run.id if run else None,
        "run_name": run.name if run else None,
        "logical_path": entry.get("logical_path"),
        "recovered_source_path": str(source),
        "backup_manifest_sha256": manifest_sha,
        "backup_manifest_uri": f"s3://{bucket}/{manifest.get('prefix')}/manifest.json" if bucket else None,
        **dict(extra_metadata or {}),
    }
    asset = models.Asset(
        workspace_id=workspace_id,
        project_id=project_id,
        kind=_archive_asset_kind(str(entry.get("kind") or "other")),
        name=str(entry.get("name") or source.name),
        mime_type=str(entry.get("content_type") or "application/octet-stream"),
        sha256=str(entry["sha256"]),
        provenance_kind="derived",
        metadata_=metadata,
    )
    db.add(asset)
    db.flush()
    _add_locations(db, workspace_id=workspace_id, asset=asset, source=managed, entry=entry, bucket=bucket)
    return asset


@router.post("/embedding-bundles/import", status_code=status.HTTP_201_CREATED)
def import_embedding_bundle(
    body: schemas.EmbeddingBundleImport,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    project = workspace_object(db, models.Project, body.project_id)
    manifest_path = Path(body.manifest_path).expanduser().resolve()
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"could not read embedding bundle manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != "modelfiche.embedding-backup/v1":
        raise HTTPException(status_code=422, detail="unsupported embedding bundle manifest schema")
    model_specs = manifest.get("models")
    files = manifest.get("files")
    if not isinstance(model_specs, dict) or not model_specs or not isinstance(files, list):
        raise HTTPException(status_code=422, detail="embedding bundle must contain models and files")
    selected = body.model_keys or list(model_specs)
    unknown = sorted(set(selected) - set(model_specs))
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown embedding model keys: {', '.join(unknown)}")

    manifest_sha = sha256(manifest_bytes).hexdigest()
    entries_by_model: dict[str, list[tuple[dict[str, Any], Path]]] = {}
    for model_key in selected:
        model_entries = [entry for entry in files if isinstance(entry, dict) and entry.get("model") == model_key]
        if sum(entry.get("kind") == "embedding" for entry in model_entries) != 1:
            raise HTTPException(status_code=422, detail=f"{model_key} must contain exactly one embedding artifact")
        entries_by_model[model_key] = [(entry, _verified_file(entry)) for entry in model_entries]

    actor = active_profile(db, x_profile_id)
    workspace_id = current_workspace(db).id
    bucket = str(manifest.get("bucket") or "")
    imported: list[dict[str, Any]] = []
    for model_key in selected:
        import_key = f"{manifest_sha}:{model_key}"
        existing = _existing_import(db, project.id, import_key)
        if existing:
            repaired = _ensure_existing_managed_locations(
                db,
                workspace_id=workspace_id,
                version=existing,
                manifest_sha=manifest_sha,
                model_key=model_key,
                entries=entries_by_model[model_key],
            )
            imported.append({
                "model_key": model_key,
                "model_id": existing.model_id,
                "model_version_id": existing.id,
                "checkpoint_id": existing.checkpoint_id,
                "image_count": int(db.scalar(select(func.count()).select_from(models.Sample).where(models.Sample.checkpoint_id == existing.checkpoint_id)) or 0),
                "created": False,
                "repaired_locations": repaired,
            })
            continue

        spec = model_specs[model_key]
        if not isinstance(spec, dict):
            raise HTTPException(status_code=422, detail=f"invalid model metadata for {model_key}")
        artifact_entry, artifact_source = next(pair for pair in entries_by_model[model_key] if pair[0].get("kind") == "embedding")
        artifact_path = _managed_copy(artifact_source, artifact_entry, manifest_sha, model_key)
        step = int(spec.get("step") or 0)
        model = models.Model(
            project_id=project.id,
            name=str(spec.get("name") or model_key),
            description=str(spec.get("notes") or "Recovered conditioning embedding artifact."),
        )
        run = models.TrainingRun(
            project_id=project.id,
            name=f"{model.name} recovered training",
            trainer="kef-embedding",
            run_kind="training",
            base_model=str(spec.get("base_model") or "") or None,
            status="succeeded",
            source_prefix=str(manifest.get("prefix") or "") or None,
            raw_manifest={"schema": manifest.get("schema"), "sha256": manifest_sha, "model_key": model_key},
            normalized_config={
                "artifact_type": "embedding",
                "artifact_format": spec.get("artifact_format"),
                "method": spec.get("method"),
                "base_model_revision": spec.get("base_model_revision"),
                "tensor_metadata": spec.get("tensor_metadata") or {},
            },
            current_step=step,
            finished_at=datetime.now(timezone.utc),
        )
        db.add_all([model, run])
        db.flush()

        artifact = models.Asset(
            workspace_id=workspace_id,
            project_id=project.id,
            kind="model",
            name=artifact_source.name,
            mime_type=str(artifact_entry.get("content_type") or "application/octet-stream"),
            sha256=str(artifact_entry["sha256"]),
            provenance_kind="derived",
            metadata_={
                "category": "model_artifact",
                "artifact_type": "embedding",
                "artifact_format": spec.get("artifact_format"),
                "method": spec.get("method"),
                "tensor_metadata": spec.get("tensor_metadata") or {},
                "backup_manifest_uri": f"s3://{bucket}/{manifest.get('prefix')}/manifest.json" if bucket else None,
                "backup_manifest_sha256": manifest_sha,
                "recovered_source_path": str(artifact_source),
            },
        )
        db.add(artifact)
        db.flush()
        _add_locations(db, workspace_id=workspace_id, asset=artifact, source=artifact_path, entry=artifact_entry, bucket=bucket)
        checkpoint = models.Checkpoint(run_id=run.id, step=step, asset_id=artifact.id, state="hydrated")
        db.add(checkpoint)
        db.flush()
        revision = establish_checkpoint_revision(db, checkpoint)
        if revision is None:
            raise HTTPException(status_code=500, detail=f"could not establish checkpoint revision for {model_key}")

        version = models.ModelVersion(
            model_id=model.id,
            checkpoint_id=checkpoint.id,
            checkpoint_revision_id=revision.id,
            name=str(spec.get("name") or model_key),
            base_model=str(spec.get("base_model") or "") or None,
            notes=str(spec.get("notes") or "") or None,
            lifecycle_state="candidate",
            artifact_type="embedding",
            artifact_format=str(spec.get("artifact_format") or "") or None,
            method=str(spec.get("method") or "") or None,
            compatibility={
                **dict(spec.get("compatibility") or {}),
                "base_model_revision": spec.get("base_model_revision"),
                "tensor_metadata": spec.get("tensor_metadata") or {},
                "import": {
                    "key": import_key,
                    "manifest_path": str(manifest_path),
                    "manifest_sha256": manifest_sha,
                    "backup_uri": f"s3://{bucket}/{manifest.get('prefix')}/manifest.json" if bucket else None,
                    "model_key": model_key,
                },
            },
        )
        db.add(version)
        db.flush()

        image_count = 0
        for entry, recovered_source in entries_by_model[model_key]:
            if entry.get("kind") != "generated_image":
                continue
            source = _managed_copy(recovered_source, entry, manifest_sha, model_key)
            provenance = dict(entry.get("provenance") or {})
            image = models.Asset(
                workspace_id=workspace_id,
                project_id=project.id,
                kind="image",
                name=recovered_source.name,
                mime_type=str(entry.get("content_type") or "image/png"),
                sha256=str(entry["sha256"]),
                provenance_kind="derived",
                metadata_={
                    **provenance,
                    "category": "sample",
                    "model_id": model.id,
                    "model_name": model.name,
                    "model_version_id": version.id,
                    "model_version_name": version.name,
                    "checkpoint_id": checkpoint.id,
                    "checkpoint_name": f"step {step}",
                    "checkpoint_revision_id": revision.id,
                    "run_id": run.id,
                    "run_name": run.name,
                    "base_model": version.base_model,
                    "artifact_type": "embedding",
                    "backup_manifest_sha256": manifest_sha,
                    "recovered_source_path": str(recovered_source),
                },
            )
            db.add(image)
            db.flush()
            _add_locations(db, workspace_id=workspace_id, asset=image, source=source, entry=entry, bucket=bucket)
            width = provenance.get("width")
            height = provenance.get("height")
            db.add(models.ImageMetadata(
                asset_id=image.id,
                width=int(width) if width is not None else None,
                height=int(height) if height is not None else None,
                color_mode=None,
                orientation=None,
                exif_summary={},
            ))
            db.add(models.Sample(
                run_id=run.id,
                checkpoint_id=checkpoint.id,
                asset_id=image.id,
                step=step,
                prompt=str(provenance.get("prompt") or "") or None,
                seed=int(provenance["seed"]) if provenance.get("seed") is not None else None,
                generation_metadata={
                    key: value
                    for key, value in provenance.items()
                    if key not in {"prompt", "seed", "width", "height"}
                },
                modified_at=datetime.fromtimestamp(recovered_source.stat().st_mtime, tz=timezone.utc),
            ))
            image_count += 1

        record_activity(
            db,
            action="embedding_bundle.imported",
            subject_type="model_version",
            subject_id=version.id,
            profile_id=actor.id,
            project_id=project.id,
            details={
                "artifact_type": "embedding",
                "model_key": model_key,
                "manifest_sha256": manifest_sha,
                "checkpoint_id": checkpoint.id,
                "image_count": image_count,
            },
        )
        imported.append({
            "model_key": model_key,
            "model_id": model.id,
            "model_version_id": version.id,
            "checkpoint_id": checkpoint.id,
            "run_id": run.id,
            "image_count": image_count,
            "created": True,
        })

    db.commit()
    return {
        "schema": manifest["schema"],
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "project_id": project.id,
        "imports": imported,
    }


@router.post("/embedding-archives/import", status_code=status.HTTP_201_CREATED)
def import_embedding_archive(
    body: schemas.EmbeddingArchiveImport,
    db: DB,
    x_profile_id: Annotated[str | None, Header(alias="X-Profile-ID")] = None,
):
    project = workspace_object(db, models.Project, body.project_id)
    manifest_path = Path(body.manifest_path).expanduser().resolve()
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"could not read embedding archive manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != "modelfiche.embedding-experiment-archive/v1":
        raise HTTPException(status_code=422, detail="unsupported embedding archive manifest schema")
    run_specs = manifest.get("runs")
    unassigned = manifest.get("unassigned", [])
    if not isinstance(run_specs, list) or not run_specs or not isinstance(unassigned, list):
        raise HTTPException(status_code=422, detail="embedding archive must contain runs and unassigned evidence")

    manifest_sha = sha256(manifest_bytes).hexdigest()
    actor = active_profile(db, x_profile_id)
    workspace_id = current_workspace(db).id
    imported: list[dict[str, Any]] = []
    for run_spec in run_specs:
        if not isinstance(run_spec, dict):
            raise HTTPException(status_code=422, detail="embedding archive run must be an object")
        run_key = str(run_spec.get("key") or "").strip()
        if not run_key:
            raise HTTPException(status_code=422, detail="embedding archive run key is required")
        import_key = f"{manifest_sha}:{run_key}"
        existing = _existing_archive_run(db, project.id, import_key)
        if existing is not None:
            version = db.scalar(
                select(models.ModelVersion)
                .join(models.Checkpoint, models.Checkpoint.id == models.ModelVersion.checkpoint_id)
                .where(models.Checkpoint.run_id == existing.id)
            )
            imported.append({
                "run_key": run_key,
                "run_id": existing.id,
                "model_version_id": version.id if version else None,
                "checkpoint_count": int(db.scalar(select(func.count()).select_from(models.Checkpoint).where(models.Checkpoint.run_id == existing.id)) or 0),
                "image_count": int(db.scalar(select(func.count()).select_from(models.Sample).where(models.Sample.run_id == existing.id)) or 0),
                "created": False,
            })
            continue

        checkpoints = run_spec.get("checkpoints", [])
        images = run_spec.get("images", [])
        evidence = run_spec.get("evidence", [])
        metric_samples = run_spec.get("metric_samples", [])
        if not all(isinstance(value, list) for value in (checkpoints, images, evidence, metric_samples)):
            raise HTTPException(status_code=422, detail=f"archive run {run_key} contains invalid entry lists")
        if any(not isinstance(entry, dict) for entry in [*checkpoints, *images, *evidence]):
            raise HTTPException(status_code=422, detail=f"archive run {run_key} contains a non-object file entry")
        family_key = str(run_spec.get("family_key") or "").strip() or None
        current_step = max((int(entry.get("step") or 0) for entry in checkpoints), default=0)
        run = models.TrainingRun(
            project_id=project.id,
            name=str(run_spec.get("name") or run_key),
            trainer=str(run_spec.get("trainer") or "kef-embedding"),
            run_kind="training",
            base_model=str(run_spec.get("base_model") or "Qwen-Image"),
            status="succeeded",
            source_prefix=str(manifest.get("prefix") or "") or None,
            raw_manifest={
                "schema": manifest["schema"],
                "manifest_sha256": manifest_sha,
                "archive_import_key": import_key,
                "archive_name": manifest.get("name"),
                "family_key": family_key,
                "run_key": run_key,
            },
            raw_state={
                "metric_summary": dict(run_spec.get("metric_summary") or {}),
                "evidence_count": len(evidence),
                "image_count": len(images),
            },
            normalized_config={
                **dict(run_spec.get("config") or {}),
                "artifact_type": "embedding",
                "archive_family_key": family_key,
                "archive_run_key": run_key,
            },
            current_step=current_step,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.flush()

        checkpoint_by_step: dict[int, models.Checkpoint] = {}
        revision_by_checkpoint: dict[str, models.CheckpointRevision] = {}
        final_entry: dict[str, Any] | None = None
        final_checkpoint: models.Checkpoint | None = None
        for entry in sorted(checkpoints, key=lambda value: int(value.get("step") or 0)):
            source = _verified_file(entry)
            managed = _archive_managed_copy(source, entry, manifest_sha)
            step = int(entry.get("step") or 0)
            asset = _create_archive_asset(
                db,
                workspace_id=workspace_id,
                project_id=project.id,
                run=run,
                entry=entry,
                source=source,
                managed=managed,
                manifest=manifest,
                manifest_sha=manifest_sha,
                family_key=family_key,
                run_key=run_key,
                extra_metadata={
                    "category": "model_artifact",
                    "checkpoint_step": step,
                    "artifact_format": entry.get("artifact_format"),
                    "method": entry.get("method") or run_spec.get("method"),
                    "tensor_metadata": dict(entry.get("tensor_metadata") or {}),
                },
            )
            checkpoint = models.Checkpoint(run_id=run.id, step=step, asset_id=asset.id, state="available")
            db.add(checkpoint)
            db.flush()
            revision = establish_checkpoint_revision(db, checkpoint)
            if revision is None:
                raise HTTPException(status_code=500, detail=f"could not establish checkpoint revision for {run_key} step {step}")
            checkpoint_by_step[step] = checkpoint
            revision_by_checkpoint[checkpoint.id] = revision
            if bool(entry.get("final")) or final_checkpoint is None or step >= final_checkpoint.step:
                final_entry = entry
                final_checkpoint = checkpoint

        model: models.Model | None = None
        version: models.ModelVersion | None = None
        if final_checkpoint is not None and final_entry is not None:
            model = models.Model(
                project_id=project.id,
                name=str(run_spec.get("name") or run_key),
                description=f"Recovered Qwen-Image embedding experiment: {family_key or run_key}.",
            )
            db.add(model)
            db.flush()
            final_revision = revision_by_checkpoint[final_checkpoint.id]
            tensor_metadata = dict(final_entry.get("tensor_metadata") or {})
            version = models.ModelVersion(
                model_id=model.id,
                checkpoint_id=final_checkpoint.id,
                checkpoint_revision_id=final_revision.id,
                name=str(run_spec.get("name") or run_key),
                base_model=str(run_spec.get("base_model") or "Qwen-Image"),
                notes=str(run_spec.get("notes") or "Recovered historical embedding training lineage."),
                lifecycle_state="candidate",
                artifact_type="embedding",
                artifact_format=str(final_entry.get("artifact_format") or run_spec.get("artifact_format") or "") or None,
                method=str(final_entry.get("method") or run_spec.get("method") or "dsci"),
                compatibility={
                    "qwen-image": {"status": "historical_training_evidence"},
                    "base_model_revision": run_spec.get("base_model_revision"),
                    "tensor_metadata": tensor_metadata,
                    "import": {
                        "key": import_key,
                        "manifest_path": str(manifest_path),
                        "manifest_sha256": manifest_sha,
                        "backup_uri": f"s3://{manifest.get('bucket')}/{manifest.get('prefix')}/manifest.json" if manifest.get("bucket") else None,
                        "family_key": family_key,
                        "run_key": run_key,
                    },
                },
            )
            db.add(version)
            db.flush()

        for entry in images:
            source = _verified_file(entry)
            managed = _archive_managed_copy(source, entry, manifest_sha)
            requested_step = int(entry.get("step") or 0)
            checkpoint = checkpoint_by_step.get(requested_step) or final_checkpoint
            revision = revision_by_checkpoint.get(checkpoint.id) if checkpoint else None
            provenance = dict(entry.get("metadata") or {})
            image = _create_archive_asset(
                db,
                workspace_id=workspace_id,
                project_id=project.id,
                run=run,
                entry=entry,
                source=source,
                managed=managed,
                manifest=manifest,
                manifest_sha=manifest_sha,
                family_key=family_key,
                run_key=run_key,
                extra_metadata={
                    "category": "sample",
                    "model_id": model.id if model else None,
                    "model_name": model.name if model else None,
                    "model_version_id": version.id if version else None,
                    "model_version_name": version.name if version else None,
                    "checkpoint_id": checkpoint.id if checkpoint else None,
                    "checkpoint_name": f"step {checkpoint.step}" if checkpoint else None,
                    "checkpoint_revision_id": revision.id if revision else None,
                    "base_model": run.base_model,
                },
            )
            width = provenance.get("width")
            height = provenance.get("height")
            db.add(models.ImageMetadata(
                asset_id=image.id,
                width=int(width) if width is not None else None,
                height=int(height) if height is not None else None,
                color_mode=str(provenance.get("color_mode") or "") or None,
                orientation=None,
                exif_summary={},
            ))
            db.add(models.Sample(
                run_id=run.id,
                checkpoint_id=checkpoint.id if checkpoint else None,
                asset_id=image.id,
                step=checkpoint.step if checkpoint else requested_step or None,
                prompt=str(provenance.get("prompt") or "") or None,
                seed=int(provenance["seed"]) if provenance.get("seed") is not None else None,
                generation_metadata={
                    key: value
                    for key, value in provenance.items()
                    if key not in {"prompt", "seed", "width", "height", "color_mode"}
                },
                modified_at=datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc),
            ))

        for entry in evidence:
            source = _verified_file(entry)
            managed = _archive_managed_copy(source, entry, manifest_sha)
            _create_archive_asset(
                db,
                workspace_id=workspace_id,
                project_id=project.id,
                run=run,
                entry=entry,
                source=source,
                managed=managed,
                manifest=manifest,
                manifest_sha=manifest_sha,
                family_key=family_key,
                run_key=run_key,
            )

        for sample in metric_samples:
            if not isinstance(sample, dict):
                continue
            step = int(sample.get("step") or 0)
            for name, value in sample.items():
                if name == "step" or value is None or not isinstance(value, (int, float)):
                    continue
                db.add(models.TrainingMetric(
                    run_id=run.id,
                    step=step,
                    name=str(name),
                    value=float(value),
                    value_text=None,
                    value_type="number",
                    source_key=f"archive:{manifest_sha}:{run_key}",
                    batch_key=manifest_sha[:32],
                ))

        record_activity(
            db,
            action="embedding_archive.run_imported",
            subject_type="training_run",
            subject_id=run.id,
            profile_id=actor.id,
            project_id=project.id,
            details={
                "manifest_sha256": manifest_sha,
                "family_key": family_key,
                "run_key": run_key,
                "checkpoint_count": len(checkpoints),
                "image_count": len(images),
                "evidence_count": len(evidence),
            },
        )
        imported.append({
            "run_key": run_key,
            "run_id": run.id,
            "model_version_id": version.id if version else None,
            "checkpoint_count": len(checkpoints),
            "image_count": len(images),
            "evidence_count": len(evidence),
            "created": True,
        })

    already_imported_unassigned = any(
        isinstance(asset.metadata_, dict)
        and asset.metadata_.get("archive_unassigned_key") == manifest_sha
        for asset in db.scalars(select(models.Asset).where(models.Asset.project_id == project.id)).all()
    )
    unassigned_count = 0
    if not already_imported_unassigned:
        for entry in unassigned:
            if not isinstance(entry, dict):
                raise HTTPException(status_code=422, detail="unassigned embedding archive evidence must be an object")
            source = _verified_file(entry)
            managed = _archive_managed_copy(source, entry, manifest_sha)
            _create_archive_asset(
                db,
                workspace_id=workspace_id,
                project_id=project.id,
                run=None,
                entry=entry,
                source=source,
                managed=managed,
                manifest=manifest,
                manifest_sha=manifest_sha,
                family_key=str(entry.get("family_key") or "") or None,
                run_key=None,
                extra_metadata={"archive_unassigned_key": manifest_sha},
            )
            unassigned_count += 1

    db.commit()
    return {
        "schema": manifest["schema"],
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "project_id": project.id,
        "imports": imported,
        "unassigned_count": unassigned_count,
    }
