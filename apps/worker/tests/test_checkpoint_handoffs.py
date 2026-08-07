from __future__ import annotations

import hashlib
import io
import json
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.database import Base
from titles_worker.checkpoint_handoffs import CheckpointHandoffReconciler, CheckpointManifestPoller


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str, str], bytes] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_get: Any = None
        self.missing: set[tuple[str, str]] = set()

    def put(self, bucket: str, key: str, value: bytes, version: str = "v1") -> None:
        self.objects[(bucket, key, version)] = value
    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list", kwargs))
        bucket, prefix = kwargs["Bucket"], kwargs["Prefix"]
        keys = sorted({key for candidate_bucket, key, _version in self.objects if candidate_bucket == bucket and key.startswith(prefix)})
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("head", kwargs))
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        version = kwargs.get("VersionId")
        if version is None:
            versions = [candidate for candidate in self.objects if candidate[:2] == (bucket, key)]
            version = versions[0][2]
        if (bucket, key) in self.missing:
            raise MissingObject()
        value = self.objects[(bucket, key, version)]
        return {"ContentLength": len(value), "ETag": hashlib.md5(value).hexdigest(), "VersionId": version}  # noqa: S324

    def get_object(self, **kwargs: Any) -> dict[str, io.BytesIO]:
        self.calls.append(("get", kwargs))
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        if self.on_get is not None:
            self.on_get(key)
        version = kwargs.get("VersionId")
        if version is None:
            versions = [candidate for candidate in self.objects if candidate[:2] == (bucket, key)]
            version = versions[0][2]
        if (bucket, key) in self.missing:
            raise MissingObject()
        return {"Body": io.BytesIO(self.objects[(bucket, key, version)])}


class MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


def _factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _graph(factory: sessionmaker[Session], *, manifest_digest: str | None = None) -> tuple[str, str, str, FakeS3, dict[str, bytes]]:
    s3 = FakeS3()
    payloads = {"step-10.safetensors": b"first-checkpoint", "step-20.safetensors": b"second-checkpoint"}
    with factory.begin() as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        dataset = models.Dataset(project_id=project.id, name="dataset")
        dataset_version = models.DatasetVersion(dataset_id=dataset.id, version_number=1, name="v1")
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="training",
            provider="s3",
            bucket="training",
            allowed_prefixes=["projects/"],
            managed_prefix="projects/",
        )
        credential = models.WandbIngestCredential(
            alias="logger",
            key_id="key-id",
            public_key="a" * 64,
        )
        run = models.TrainingRun(project_id=project.id, dataset_version_id=dataset_version.id, name="run", base_model="krea-2", status="completed")
        session.add_all([workspace, project, dataset, dataset_version, source, credential, run])
        session.flush()
        launch = models.TrainingLaunch(
            workspace_id=workspace.id,
            project_id=project.id,
            run_id=run.id,
            dataset_version_id=dataset_version.id,
            wandb_credential_id=credential.id,
            client_request_id="request-1",
            source_id=source.id,
            run_prefix=f"projects/{project.id}/runs/{run.id}/",
            supported_endpoint_ids=["fal-ai/krea-2/turbo/lora"],
        )
        manifest_key = f"{launch.run_prefix}manifests/checkpoints/1.json"
        entries = [
            {
                "object_key": f"{launch.run_prefix}checkpoints/{name}",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "step": step,
                "name": name,
            }
            for (name, payload), step in zip(payloads.items(), (10, 20))
        ]
        manifest = {"schema_version": "modelfiche.checkpoint-manifest.v1", "run_id": run.id, "generation": 1, "checkpoints": entries}
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
        s3.put(source.bucket, manifest_key, manifest_bytes, "manifest-v7")
        for entry, payload in zip(entries, payloads.values()):
            s3.put(source.bucket, entry["object_key"], payload, "object-v1")
        handoff = models.RunArtifactHandoff(
            run_id=run.id,
            generation=1,
            manifest_key=manifest_key,
            manifest_etag=hashlib.md5(manifest_bytes).hexdigest(),  # noqa: S324
            manifest_version_id="manifest-v7",
            manifest_digest=manifest_digest or hashlib.sha256(manifest_bytes).hexdigest(),
            source_fingerprint="source-fingerprint",
            checkpoint_count=2,
            state="observed",
        )
        session.add_all([launch, handoff])
        session.flush()
        return handoff.id, run.id, source.id, s3, {"manifest": manifest_bytes, **payloads}


def test_manifest_last_imports_two_checkpoints_and_uses_manifest_version() -> None:
    factory = _factory()
    handoff_id, run_id, _, s3, _ = _graph(factory)
    with factory.begin() as session:
        run = session.get(models.TrainingRun, run_id)
        assert run is not None
        placeholder = models.Model(project_id=run.project_id, name=run.name)
        session.add(placeholder)
        session.flush()
        placeholder_id = placeholder.id
    assert CheckpointHandoffReconciler(factory, client_factory=lambda _source: s3).run_once() == 1
    with factory() as session:
        handoff = session.get(models.RunArtifactHandoff, handoff_id)
        assert handoff is not None and handoff.state == "imported"
        assert handoff.verified_count == handoff.imported_count == 2
        checkpoints = list(session.scalars(select(models.Checkpoint).where(models.Checkpoint.run_id == run_id)))
        assert len(checkpoints) == 2
        assert all(checkpoint.current_revision_id for checkpoint in checkpoints)
        locations = list(session.scalars(select(models.AssetLocation)))
        assert len(locations) == 2
        assert all(location.verification_state == "verified" and location.version_id == "object-v1" for location in locations)
        model = session.scalar(select(models.Model).where(models.Model.description == f"Imported from training run {run_id}"))
        assert model is not None
        assert model.id == placeholder_id
        assert len(list(session.scalars(select(models.Model)))) == 1
        versions = list(session.scalars(select(models.ModelVersion).where(models.ModelVersion.model_id == model.id)))
        assert len(versions) == 2
        assert all(version.lifecycle_state == "candidate" and version.readiness["fal_status"] == "not_ready" for version in versions)
        assert all("fal_url" not in version.readiness for version in versions)
        assert {event.type for event in session.scalars(select(models.RunEvent).where(models.RunEvent.run_id == run_id))} >= {
            "run.checkpoint_manifest.verified",
            "run.checkpoint_manifest.imported",
        }
    manifest_gets = [kwargs for kind, kwargs in s3.calls if kind == "get" and "manifests/" in kwargs["Key"]]
    assert manifest_gets and manifest_gets[0]["VersionId"] == "manifest-v7"

    assert CheckpointHandoffReconciler(factory, client_factory=lambda _source: s3).run_once() == 0
    with factory() as session:
        assert session.scalar(select(models.Checkpoint).where(models.Checkpoint.run_id == run_id)) is not None
        assert len(list(session.scalars(select(models.CheckpointRevision)))) == 2
        assert len(list(session.scalars(select(models.ModelVersion)))) == 2
        assert len(list(session.scalars(select(models.RunEvent).where(models.RunEvent.run_id == run_id)))) == 2


def test_s3_poller_discovers_and_imports_manifest_without_callback() -> None:
    factory = _factory()
    handoff_id, run_id, _, s3, _ = _graph(factory)
    with factory.begin() as session:
        session.delete(session.get(models.RunArtifactHandoff, handoff_id))

    assert CheckpointManifestPoller(factory, client_factory=lambda _source: s3).run_once() == 1
    with factory() as session:
        handoff = session.scalar(select(models.RunArtifactHandoff).where(models.RunArtifactHandoff.run_id == run_id))
        assert handoff is not None and handoff.state == "observed"
        observed = session.scalar(
            select(models.RunEvent).where(
                models.RunEvent.run_id == run_id,
                models.RunEvent.type == "run.checkpoint_manifest.observed",
            )
        )
        assert observed is not None and observed.payload["transport"] == "s3_poll"

    assert CheckpointHandoffReconciler(factory, client_factory=lambda _source: s3).run_once() == 1
    with factory() as session:
        handoff = session.scalar(select(models.RunArtifactHandoff).where(models.RunArtifactHandoff.run_id == run_id))
        assert handoff is not None and handoff.state == "imported"


def test_remote_reads_do_not_hold_sqlite_writer_lock(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'handoffs.sqlite3'}", connect_args={"timeout": 0.1})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _, _, _, s3, _ = _graph(factory)
    write_observed = False

    def write_during_second_checkpoint(key: str) -> None:
        nonlocal write_observed
        if write_observed or not key.endswith("step-20.safetensors"):
            return
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE training_runs SET name = name")
        write_observed = True

    s3.on_get = write_during_second_checkpoint
    assert CheckpointHandoffReconciler(factory, client_factory=lambda _source: s3).run_once() == 1
    assert write_observed


def test_manifest_digest_failure_imports_nothing() -> None:
    factory = _factory()
    handoff_id, run_id, _, s3, _ = _graph(factory, manifest_digest="0" * 64)
    assert CheckpointHandoffReconciler(factory, client_factory=lambda _source: s3).run_once() == 1
    with factory() as session:
        handoff = session.get(models.RunArtifactHandoff, handoff_id)
        assert handoff is not None and handoff.state == "failed"
        assert "digest" in (handoff.error or "")
        assert session.scalar(select(models.Checkpoint).where(models.Checkpoint.run_id == run_id)) is None
        assert session.scalar(select(models.Asset)) is None


def test_missing_checkpoint_object_remains_retryable() -> None:
    factory = _factory()
    handoff_id, run_id, _, s3, _ = _graph(factory)
    with factory() as session:
        launch = session.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == run_id))
        assert launch is not None
        s3.missing.add(("training", f"{launch.run_prefix}checkpoints/step-20.safetensors"))
    assert CheckpointHandoffReconciler(factory, client_factory=lambda _source: s3).run_once() == 0
    with factory() as session:
        handoff = session.get(models.RunArtifactHandoff, handoff_id)
        assert handoff is not None and handoff.state == "reconciling"
        assert (handoff.error or "").startswith("transient:")
        assert session.scalar(select(models.Checkpoint).where(models.Checkpoint.run_id == run_id)) is None
