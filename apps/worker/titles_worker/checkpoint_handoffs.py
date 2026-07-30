from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
import json
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.checkpoint_revisions import establish_checkpoint_revision
from titles_api.integrations.s3.sqlalchemy_sink import _supported_fal_endpoint
from titles_api.storage.s3_client import create_source_s3_client
from titles_api.training_metrics import append_run_event
from titles_api.wandb_uploads import source_settings


CHECKPOINT_MANIFEST_VERSION = "modelfiche.checkpoint-manifest.v1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
# A checkpoint may be large, but an unbounded remote response must never be read.
MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024 * 1024
RETRY_DELAY = timedelta(seconds=30)

ClientFactory = Callable[[models.ImportSource], Any]


class DeterministicHandoffError(ValueError):
    """The source was reachable, but the immutable handoff contract was invalid."""


class TransientHandoffError(RuntimeError):
    """The source/client was unavailable; the handoff remains retryable."""


def _client_for_source(source: models.ImportSource) -> Any:
    return create_source_s3_client(source_settings(source))


def _is_missing_object(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return isinstance(error, dict) and str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"}


def _request_args(bucket: str, key: str, version_id: str | None = None) -> dict[str, str]:
    result = {"Bucket": bucket, "Key": key}
    if version_id:
        result["VersionId"] = version_id
    return result


def _read_stream(body: Any, *, limit: int, collect: bool = False) -> tuple[bytes, int, str]:
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    try:
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise DeterministicHandoffError("source object returned a non-byte body")
            value = bytes(chunk)
            size += len(value)
            if size > limit:
                raise DeterministicHandoffError("source object exceeds the configured size bound")
            digest.update(value)
            if collect:
                chunks.append(value)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    return (b"".join(chunks) if collect else b""), size, digest.hexdigest()


def _head(client: Any, bucket: str, key: str, *, version_id: str | None = None) -> dict[str, Any]:
    try:
        response = client.head_object(**_request_args(bucket, key, version_id))
    except Exception as exc:
        if _is_missing_object(exc):
            raise TransientHandoffError("source object is unavailable") from exc
        raise TransientHandoffError("source client request failed") from exc
    if not isinstance(response, dict):
        raise DeterministicHandoffError("source head response is invalid")
    return response


def _get(client: Any, bucket: str, key: str, *, version_id: str | None = None) -> dict[str, Any]:
    try:
        response = client.get_object(**_request_args(bucket, key, version_id))
    except Exception as exc:
        if _is_missing_object(exc):
            raise TransientHandoffError("source object is unavailable") from exc
        raise TransientHandoffError("source client request failed") from exc
    if not isinstance(response, dict) or "Body" not in response:
        raise DeterministicHandoffError("source get response is invalid")
    return response


def _check_head_size(head: dict[str, Any], *, limit: int, label: str) -> None:
    value = head.get("ContentLength")
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DeterministicHandoffError(f"{label} size is invalid")
    if value > limit:
        raise DeterministicHandoffError(f"{label} exceeds the configured size bound")


def _etag(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value.strip('"') or None


def _validate_manifest(
    raw: bytes,
    *,
    run_id: str,
    generation: int,
    checkpoint_count: int,
    checkpoint_prefix: str,
) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DeterministicHandoffError("checkpoint manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise DeterministicHandoffError("checkpoint manifest must be an object")
    if set(manifest) != {"schema_version", "run_id", "generation", "checkpoints"}:
        raise DeterministicHandoffError("checkpoint manifest contains unknown fields")
    if manifest.get("schema_version") != CHECKPOINT_MANIFEST_VERSION:
        raise DeterministicHandoffError("unsupported checkpoint manifest schema version")
    if manifest.get("run_id") != run_id:
        raise DeterministicHandoffError("checkpoint manifest run identity does not match launch")
    manifest_generation = manifest.get("generation")
    if isinstance(manifest_generation, bool) or not isinstance(manifest_generation, int) or manifest_generation != generation or manifest_generation < 1:
        raise DeterministicHandoffError("checkpoint manifest generation does not match handoff")
    checkpoints = manifest.get("checkpoints")
    if not isinstance(checkpoints, list) or len(checkpoints) != checkpoint_count:
        raise DeterministicHandoffError("checkpoint manifest count does not match handoff")
    expected_prefix = checkpoint_prefix
    seen_keys: set[str] = set()
    seen_steps: set[int] = set()
    validated: list[dict[str, Any]] = []
    for entry in checkpoints:
        if not isinstance(entry, dict):
            raise DeterministicHandoffError("checkpoint manifest entry must be an object")
        if set(entry) - {"object_key", "sha256", "size", "step", "name"}:
            raise DeterministicHandoffError("checkpoint manifest entry contains unknown fields")
        key = entry.get("object_key")
        digest = entry.get("sha256")
        size = entry.get("size")
        step = entry.get("step")
        if not isinstance(key, str) or not key or "\\" in key or "\x00" in key or not key.startswith(expected_prefix):
            raise DeterministicHandoffError("checkpoint object key is outside its launch prefix")
        if PurePosixPath(key).parts[-1] in {"", ".", ".."} or "/../" in f"/{key}/":
            raise DeterministicHandoffError("checkpoint object key is invalid")
        if key in seen_keys:
            raise DeterministicHandoffError("checkpoint manifest contains duplicate object keys")
        if not isinstance(digest, str) or len(digest) != 64 or digest != digest.lower() or any(c not in "0123456789abcdef" for c in digest):
            raise DeterministicHandoffError("checkpoint SHA-256 is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_CHECKPOINT_BYTES:
            raise DeterministicHandoffError("checkpoint size is invalid")
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise DeterministicHandoffError("checkpoint step is invalid")
        if step in seen_steps:
            raise DeterministicHandoffError("checkpoint manifest contains duplicate steps")
        name = entry.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise DeterministicHandoffError("checkpoint name is invalid")
        seen_keys.add(key)
        seen_steps.add(step)
        validated.append({"object_key": key, "sha256": digest, "size": size, "step": step, "name": name})
    return validated


def _fingerprint(source: models.ImportSource, key: str, version_id: str | None, digest: str) -> str:
    value = f"{source.id}\0{key}\0{version_id or ''}\0{digest}"
    return hashlib.sha256(value.encode()).hexdigest()


def _materialize_checkpoint(
    session: Session,
    *,
    run: models.TrainingRun,
    launch: models.TrainingLaunch,
    source: models.ImportSource,
    descriptor: dict[str, Any],
    head: dict[str, Any],
    digest: str,
    size: int,
) -> models.Checkpoint:
    key = descriptor["object_key"]
    version_id = head.get("VersionId")
    if version_id is not None and not isinstance(version_id, str):
        raise DeterministicHandoffError("checkpoint source version is invalid")
    fingerprint = _fingerprint(source, key, version_id, digest)
    asset = session.scalar(
        select(models.Asset)
        .where(
            models.Asset.workspace_id == launch.workspace_id,
            models.Asset.project_id == run.project_id,
            models.Asset.kind == models.AssetKind.model,
            models.Asset.sha256 == digest,
        )
        .order_by(models.Asset.created_at.asc())
    )
    if asset is None:
        asset = models.Asset(
            workspace_id=launch.workspace_id,
            project_id=run.project_id,
            kind=models.AssetKind.model,
            name=descriptor.get("name") or PurePosixPath(key).name,
            mime_type="application/octet-stream",
            sha256=digest,
            provenance_kind="training_checkpoint",
            metadata_={"origin": "training_checkpoint", "training_run_id": run.id},
        )
        session.add(asset)
        session.flush()
    blob = session.scalar(select(models.ContentBlob).where(models.ContentBlob.sha256 == digest))
    if blob is None:
        blob = models.ContentBlob(sha256=digest, size=size, mime_type="application/octet-stream", verified_at=models.utcnow())
        session.add(blob)
        session.flush()
    elif blob.size != size:
        raise DeterministicHandoffError("checkpoint digest is associated with a conflicting size")
    asset.content_blob_id = blob.id
    location = session.scalar(
        select(models.AssetLocation).where(
            models.AssetLocation.workspace_id == launch.workspace_id,
            models.AssetLocation.source_id == source.id,
            models.AssetLocation.object_key == key,
            models.AssetLocation.source_revision_fingerprint == fingerprint,
        )
    )
    now = models.utcnow()
    if location is None:
        location = models.AssetLocation(
            asset_id=asset.id,
            workspace_id=launch.workspace_id,
            provider="s3",
            uri=f"s3://{source.bucket}/{key}",
            bucket=source.bucket,
            object_key=key,
            etag=_etag(head.get("ETag")),
            version_id=version_id,
            source_id=source.id,
            source_revision_fingerprint=fingerprint,
            size=size,
            verified_size=size,
            verified_sha256=digest,
            verification_state="verified",
            last_verified_at=now,
            hydration_state="remote",
        )
        session.add(location)
        session.flush()
    else:
        location.asset_id = asset.id
        location.etag = _etag(head.get("ETag"))
        location.version_id = version_id
        location.size = size
        location.verified_size = size
        location.verified_sha256 = digest
        location.verification_state = "verified"
        location.last_verified_at = now
        location.last_seen_at = now
    asset.preferred_location_id = location.id
    asset.origin_location_id = asset.origin_location_id or location.id
    checkpoint = session.scalar(
        select(models.Checkpoint).where(models.Checkpoint.run_id == run.id, models.Checkpoint.step == descriptor["step"]).order_by(models.Checkpoint.created_at.asc())
    )
    if checkpoint is None:
        checkpoint = models.Checkpoint(run_id=run.id, step=descriptor["step"], asset_id=asset.id, state="available")
        session.add(checkpoint)
        session.flush()
    else:
        checkpoint.asset_id = asset.id
        checkpoint.state = "available"
    current = session.get(models.CheckpointRevision, checkpoint.current_revision_id) if checkpoint.current_revision_id else None
    revision = establish_checkpoint_revision(session, checkpoint, location=location, force_new=bool(current and current.source_location_id != location.id))
    if revision is None:
        raise DeterministicHandoffError("verified checkpoint location did not produce a revision")
    return checkpoint


def _reconcile_model(session: Session, run: models.TrainingRun, launch: models.TrainingLaunch, checkpoints: list[models.Checkpoint]) -> None:
    marker = f"Imported from training run {run.id}"
    model = session.scalar(select(models.Model).where(models.Model.project_id == run.project_id, models.Model.description == marker))
    if model is None:
        model = session.scalar(
            select(models.Model)
            .where(
                models.Model.project_id == run.project_id,
                models.Model.name == run.name,
                models.Model.description.is_(None),
                ~select(models.ModelVersion.id).where(models.ModelVersion.model_id == models.Model.id).exists(),
            )
            .order_by(models.Model.created_at, models.Model.id)
        )
    if model is None:
        model = models.Model(project_id=run.project_id, name=run.name, description=marker)
        session.add(model)
        session.flush()
    else:
        model.name = run.name
        model.description = marker
    supported = {str(value) for value in (launch.supported_endpoint_ids or []) if isinstance(value, str)}
    compatible_endpoint = _supported_fal_endpoint(run.base_model)
    for checkpoint in checkpoints:
        version = session.scalar(select(models.ModelVersion).where(models.ModelVersion.checkpoint_id == checkpoint.id))
        asset = session.get(models.Asset, checkpoint.asset_id)
        if version is None:
            version = models.ModelVersion(
                model_id=model.id,
                checkpoint_id=checkpoint.id,
                name=PurePosixPath(asset.name).stem if asset else f"step-{checkpoint.step}",
                base_model=run.base_model,
                lifecycle_state="candidate",
                readiness={},
            )
            session.add(version)
        else:
            version.model_id = model.id
            version.base_model = run.base_model
            version.lifecycle_state = "candidate"
        readiness = {**dict(version.readiness or {}), "imported": True, "s3_verified": True, "training_run_id": run.id}
        fal_url = readiness.get("fal_url")
        if compatible_endpoint and compatible_endpoint in supported:
            readiness["endpoint_id"] = compatible_endpoint
        else:
            readiness.pop("endpoint_id", None)
        readiness["fal_status"] = "ready" if fal_url and compatible_endpoint and compatible_endpoint in supported else "not_ready"
        version.readiness = readiness
        version.checkpoint_revision_id = checkpoint.current_revision_id


@dataclass(frozen=True, slots=True)
class _VerifiedCheckpoint:
    descriptor: dict[str, Any]
    head: dict[str, Any]
    digest: str
    size: int


class CheckpointManifestPoller:
    """Discover immutable checkpoint manifests without requiring live ingress."""

    def __init__(self, session_factory: sessionmaker[Session], *, client_factory: ClientFactory = _client_for_source) -> None:
        self.session_factory = session_factory
        self.client_factory = client_factory

    def run_once(self, *, limit: int = 25) -> int:
        with self.session_factory() as session:
            launch_ids = list(
                session.scalars(
                    select(models.TrainingLaunch.id)
                    .where(
                        models.TrainingLaunch.source_id.is_not(None),
                        models.TrainingLaunch.state != "canceled",
                        or_(
                            models.TrainingLaunch.state.in_(("pending", "running", "interrupted")),
                            models.TrainingLaunch.backup_status.is_(None),
                            models.TrainingLaunch.backup_status != "completed",
                        ),
                    )
                    .order_by(models.TrainingLaunch.updated_at.desc(), models.TrainingLaunch.id)
                    .limit(limit)
                )
            )
        discovered = 0
        for launch_id in launch_ids:
            discovered += self._discover_launch(launch_id, limit=max(0, limit - discovered))
            if discovered >= limit:
                break
        return discovered

    def _discover_launch(self, launch_id: str, *, limit: int) -> int:
        if limit < 1:
            return 0
        with self.session_factory() as session:
            launch = session.get(models.TrainingLaunch, launch_id)
            run = session.get(models.TrainingRun, launch.run_id) if launch else None
            source = session.get(models.ImportSource, launch.source_id) if launch and launch.source_id else None
            if launch is None or run is None or source is None or not source.is_active:
                return 0
            client = self.client_factory(source)
            bucket = source.bucket
            source_id = source.id
            run_id = run.id
            run_prefix = launch.run_prefix
        manifest_prefix = f"{run_prefix}manifests/checkpoints/"
        keys: dict[int, str] = {}
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {"Bucket": bucket, "Prefix": manifest_prefix, "MaxKeys": 1000}
            if continuation:
                request["ContinuationToken"] = continuation
            try:
                page = client.list_objects_v2(**request)
            except Exception as exc:
                raise TransientHandoffError("checkpoint manifest listing failed") from exc
            for item in page.get("Contents", []):
                key = item.get("Key") if isinstance(item, dict) else None
                if not isinstance(key, str) or not key.startswith(manifest_prefix) or not key.endswith(".json"):
                    continue
                generation_text = key[len(manifest_prefix) : -5]
                if generation_text.isdigit() and int(generation_text) > 0 and "/" not in generation_text:
                    keys[int(generation_text)] = key
            continuation_value = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not isinstance(continuation_value, str) or not continuation_value:
                break
            continuation = continuation_value
        if not keys:
            return 0
        with self.session_factory() as session:
            existing = set(
                session.scalars(
                    select(models.RunArtifactHandoff.generation).where(
                        models.RunArtifactHandoff.run_id == run_id,
                        models.RunArtifactHandoff.generation.in_(keys),
                    )
                )
            )
        discovered = 0
        for generation, manifest_key in sorted(keys.items()):
            if generation in existing or discovered >= limit:
                continue
            manifest_head = _head(client, bucket, manifest_key)
            _check_head_size(manifest_head, limit=MAX_MANIFEST_BYTES, label="checkpoint manifest")
            manifest_version = manifest_head.get("VersionId")
            if manifest_version is not None and (not isinstance(manifest_version, str) or not manifest_version):
                raise DeterministicHandoffError("checkpoint manifest version is invalid")
            response = _get(client, bucket, manifest_key, version_id=manifest_version)
            raw, manifest_size, manifest_digest = _read_stream(response["Body"], limit=MAX_MANIFEST_BYTES, collect=True)
            if manifest_size != manifest_head.get("ContentLength", manifest_size):
                raise DeterministicHandoffError("checkpoint manifest size changed")
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise DeterministicHandoffError("checkpoint manifest is not valid JSON") from exc
            checkpoints = payload.get("checkpoints") if isinstance(payload, dict) else None
            checkpoint_count = len(checkpoints) if isinstance(checkpoints, list) else -1
            _validate_manifest(
                raw,
                run_id=run_id,
                generation=generation,
                checkpoint_count=checkpoint_count,
                checkpoint_prefix=f"{run_prefix}checkpoints/",
            )
            manifest_etag = _etag(manifest_head.get("ETag"))
            if manifest_etag is None and manifest_version is None:
                raise DeterministicHandoffError("checkpoint manifest has no immutable source revision")
            source_value = f"{source_id}\0{manifest_key}\0{manifest_version or manifest_etag or ''}\0{manifest_digest}"
            now = models.utcnow()
            with self.session_factory.begin() as session:
                duplicate = session.scalar(
                    select(models.RunArtifactHandoff.id).where(
                        models.RunArtifactHandoff.run_id == run_id,
                        models.RunArtifactHandoff.generation == generation,
                    )
                )
                if duplicate is not None:
                    continue
                launch = session.get(models.TrainingLaunch, launch_id)
                run = session.get(models.TrainingRun, run_id)
                if launch is None or run is None or launch.source_id != source_id or launch.run_prefix != run_prefix:
                    raise TransientHandoffError("launch source changed during checkpoint discovery")
                handoff = models.RunArtifactHandoff(
                    run_id=run_id,
                    generation=generation,
                    manifest_key=manifest_key,
                    manifest_etag=manifest_etag,
                    manifest_version_id=manifest_version,
                    manifest_digest=manifest_digest,
                    state="observed",
                    source_fingerprint=hashlib.sha256(source_value.encode("utf-8")).hexdigest(),
                    checkpoint_count=checkpoint_count,
                    observed_at=now,
                )
                session.add(handoff)
                launch.backup_status = "reconciling"
                launch.checkpoint_handoff_status = "observed"
                launch.next_reconcile_at = now
                append_run_event(
                    session,
                    run,
                    type="run.checkpoint_manifest.observed",
                    idempotency_key=f"checkpoint-manifest:{run_id}:{generation}:{manifest_digest}",
                    payload={
                        "handoff_id": handoff.id,
                        "generation": generation,
                        "manifest_key": manifest_key,
                        "checkpoint_count": checkpoint_count,
                        "transport": "s3_poll",
                    },
                    step=run.current_step,
                    occurred_at=now,
                )
            discovered += 1
        return discovered


class CheckpointHandoffReconciler:
    def __init__(self, session_factory: sessionmaker[Session], *, client_factory: ClientFactory = _client_for_source) -> None:
        self.session_factory = session_factory
        self.client_factory = client_factory

    def run_once(self, *, limit: int = 25) -> int:
        now = models.utcnow()
        with self.session_factory() as session:
            handoff_ids = list(
                session.scalars(
                    select(models.RunArtifactHandoff.id)
                    .join(
                        models.TrainingLaunch,
                        models.TrainingLaunch.run_id == models.RunArtifactHandoff.run_id,
                    )
                    .where(
                        models.RunArtifactHandoff.state.in_(("observed", "reconciling", "verifying", "partial")),
                        (
                            models.TrainingLaunch.next_reconcile_at.is_(None)
                            | (models.TrainingLaunch.next_reconcile_at <= now)
                        ),
                    )
                    .order_by(models.RunArtifactHandoff.observed_at, models.RunArtifactHandoff.id)
                    .limit(limit)
                )
            )
        completed = 0
        for handoff_id in handoff_ids:
            try:
                self._reconcile(handoff_id)
                completed += 1
            except DeterministicHandoffError as exc:
                self._record_failure(handoff_id, str(exc))
                completed += 1
            except Exception as exc:
                self._record_transient_error(handoff_id, exc)
        return completed

    def _reconcile(self, handoff_id: str) -> None:
        # Claim the handoff in a short transaction. Remote checkpoint reads can
        # take minutes and must not hold SQLite's single-writer lock.
        with self.session_factory.begin() as session:
            handoff = session.get(models.RunArtifactHandoff, handoff_id, with_for_update=True)
            if handoff is None or handoff.state not in {"observed", "reconciling", "verifying", "partial"}:
                return
            run = session.get(models.TrainingRun, handoff.run_id)
            launch = session.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == handoff.run_id))
            source = session.get(models.ImportSource, launch.source_id) if launch and launch.source_id else None
            if run is None or launch is None or source is None or not source.is_active:
                raise TransientHandoffError("launch source is unavailable")
            handoff.state = "verifying"
            launch.checkpoint_handoff_status = "verifying"
            client = self.client_factory(source)
            bucket = source.bucket
            run_id = run.id
            generation = handoff.generation
            checkpoint_count = handoff.checkpoint_count
            manifest_key = handoff.manifest_key
            manifest_etag = handoff.manifest_etag
            manifest_version_id = handoff.manifest_version_id
            manifest_digest_expected = handoff.manifest_digest
            run_prefix = launch.run_prefix

        expected_manifest_key = f"{run_prefix}manifests/checkpoints/{generation}.json"
        if manifest_key != expected_manifest_key:
            raise DeterministicHandoffError("checkpoint manifest key does not match launch generation")
        manifest_head = _head(client, bucket, manifest_key, version_id=manifest_version_id)
        _check_head_size(manifest_head, limit=MAX_MANIFEST_BYTES, label="checkpoint manifest")
        if manifest_version_id and manifest_head.get("VersionId") not in {None, manifest_version_id}:
            raise DeterministicHandoffError("checkpoint manifest version does not match handoff")
        actual_etag = _etag(manifest_head.get("ETag"))
        if manifest_etag and actual_etag and actual_etag != _etag(manifest_etag):
            raise DeterministicHandoffError("checkpoint manifest ETag does not match handoff")
        manifest_version = manifest_version_id or manifest_head.get("VersionId")
        response = _get(client, bucket, manifest_key, version_id=manifest_version)
        if (
            manifest_version is not None
            and response.get("VersionId") is not None
            and response.get("VersionId") != manifest_version
        ):
            raise DeterministicHandoffError("checkpoint manifest response version changed")
        raw, manifest_size, manifest_digest = _read_stream(response["Body"], limit=MAX_MANIFEST_BYTES, collect=True)
        if manifest_size != manifest_head.get("ContentLength", manifest_size):
            raise DeterministicHandoffError("checkpoint manifest size changed")
        if manifest_digest != manifest_digest_expected:
            raise DeterministicHandoffError("checkpoint manifest digest does not match handoff")
        descriptors = _validate_manifest(
            raw,
            run_id=run_id,
            generation=generation,
            checkpoint_count=checkpoint_count,
            checkpoint_prefix=f"{run_prefix}checkpoints/",
        )
        verified: list[_VerifiedCheckpoint] = []
        for descriptor in descriptors:
            object_head = _head(client, bucket, descriptor["object_key"])
            _check_head_size(object_head, limit=MAX_CHECKPOINT_BYTES, label="checkpoint object")
            head_size = object_head.get("ContentLength")
            if head_size is not None and head_size != descriptor["size"]:
                raise DeterministicHandoffError("checkpoint object size does not match manifest")
            object_version = object_head.get("VersionId")
            if object_version is not None and (not isinstance(object_version, str) or not object_version):
                raise DeterministicHandoffError("checkpoint source version is invalid")
            response = _get(client, bucket, descriptor["object_key"], version_id=object_version)
            if (
                object_version is not None
                and response.get("VersionId") is not None
                and response.get("VersionId") != object_version
            ):
                raise DeterministicHandoffError("checkpoint object response version changed")
            _, size, digest = _read_stream(response["Body"], limit=MAX_CHECKPOINT_BYTES)
            if size != descriptor["size"] or digest != descriptor["sha256"]:
                raise DeterministicHandoffError("checkpoint object digest or size does not match manifest")
            verified.append(_VerifiedCheckpoint(descriptor=descriptor, head=object_head, digest=digest, size=size))

        # Materialize only after all remote bytes have been verified. A failed
        # read therefore leaves no partial checkpoint graph.
        with self.session_factory.begin() as session:
            handoff = session.get(models.RunArtifactHandoff, handoff_id, with_for_update=True)
            if handoff is None or handoff.state not in {"observed", "reconciling", "verifying", "partial"}:
                return
            run = session.get(models.TrainingRun, handoff.run_id)
            launch = session.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == handoff.run_id))
            source = session.get(models.ImportSource, launch.source_id) if launch and launch.source_id else None
            if run is None or launch is None or source is None or not source.is_active:
                raise TransientHandoffError("launch source is unavailable")
            if (
                run.id != run_id
                or handoff.generation != generation
                or handoff.manifest_key != manifest_key
                or handoff.manifest_digest != manifest_digest_expected
                or launch.run_prefix != run_prefix
                or source.bucket != bucket
            ):
                raise TransientHandoffError("launch source changed during checkpoint verification")
            checkpoints: list[models.Checkpoint] = []
            for item in verified:
                checkpoint = _materialize_checkpoint(
                    session,
                    run=run,
                    launch=launch,
                    source=source,
                    descriptor=item.descriptor,
                    head=item.head,
                    digest=item.digest,
                    size=item.size,
                )
                checkpoints.append(checkpoint)
            _reconcile_model(session, run, launch, checkpoints)
            now = models.utcnow()
            handoff.state = "imported"
            handoff.error = None
            handoff.checkpoint_count = len(descriptors)
            handoff.verified_count = len(verified)
            handoff.imported_count = len(checkpoints)
            handoff.failed_count = 0
            handoff.verified_at = now
            handoff.imported_at = now
            launch.backup_status = "completed"
            launch.checkpoint_handoff_status = "imported"
            launch.next_reconcile_at = None
            launch.last_reconcile_at = now
            launch.reconcile_generation = handoff.generation
            launch.reconcile_error = None
            append_run_event(
                session,
                run,
                type="run.checkpoint_manifest.verified",
                idempotency_key=f"checkpoint-manifest:verified:{handoff.id}:{handoff.manifest_digest}",
                payload={"handoff_id": handoff.id, "generation": handoff.generation, "verified_count": handoff.verified_count},
                occurred_at=now,
            )
            append_run_event(
                session,
                run,
                type="run.checkpoint_manifest.imported",
                idempotency_key=f"checkpoint-manifest:imported:{handoff.id}:{handoff.manifest_digest}",
                payload={"handoff_id": handoff.id, "generation": handoff.generation, "imported_count": handoff.imported_count},
                occurred_at=now,
            )

    def _record_failure(self, handoff_id: str, message: str) -> None:
        with self.session_factory.begin() as session:
            handoff = session.get(models.RunArtifactHandoff, handoff_id, with_for_update=True)
            if handoff is None or handoff.state == "imported":
                return
            run = session.get(models.TrainingRun, handoff.run_id)
            launch = session.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == handoff.run_id))
            now = models.utcnow()
            handoff.state = "failed"
            handoff.error = message[:500]
            handoff.failed_count = handoff.checkpoint_count
            handoff.imported_count = 0
            if launch is not None:
                launch.checkpoint_handoff_status = "failed"
                launch.backup_status = "failed"
                launch.last_reconcile_at = now
                launch.next_reconcile_at = None
                launch.reconcile_error = message[:500]
            if run is not None:
                append_run_event(
                    session,
                    run,
                    type="run.checkpoint_manifest.failed",
                    idempotency_key=f"checkpoint-manifest:failed:{handoff.id}:{handoff.manifest_digest}",
                    payload={"handoff_id": handoff.id, "generation": handoff.generation, "error": message[:500]},
                    occurred_at=now,
                )

    def _record_transient_error(self, handoff_id: str, exc: Exception) -> None:
        with self.session_factory.begin() as session:
            handoff = session.get(models.RunArtifactHandoff, handoff_id, with_for_update=True)
            if handoff is None or handoff.state in {"imported", "failed"}:
                return
            run = session.get(models.TrainingRun, handoff.run_id)
            launch = session.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == handoff.run_id))
            now = models.utcnow()
            safe_error = f"transient:{type(exc).__name__}"
            handoff.state = "reconciling"
            handoff.error = safe_error
            if launch is not None:
                launch.checkpoint_handoff_status = "reconciling"
                launch.backup_status = "reconciling"
                launch.last_reconcile_at = now
                launch.next_reconcile_at = now + RETRY_DELAY
                launch.reconcile_error = safe_error
            if run is not None:
                append_run_event(
                    session,
                    run,
                    type="run.checkpoint_manifest.retryable",
                    idempotency_key=f"checkpoint-manifest:retryable:{handoff.id}:{type(exc).__name__}",
                    payload={"handoff_id": handoff.id, "generation": handoff.generation, "error_type": type(exc).__name__},
                    occurred_at=now,
                )


__all__ = ["CheckpointHandoffReconciler", "DeterministicHandoffError", "TransientHandoffError"]
