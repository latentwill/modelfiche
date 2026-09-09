from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.database import Base
from titles_worker.wandb_uploads import WandbUploadReconciler


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: Any, **_kwargs: Any) -> dict[str, str]:
        value = Body.read()
        self.objects[(Bucket, Key)] = value
        return {"ETag": hashlib.md5(value).hexdigest(), "VersionId": "version-1"}  # noqa: S324

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        value = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(value),
            "ETag": hashlib.md5(value).hexdigest(),  # noqa: S324
            "VersionId": "version-1",
        }

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, io.BytesIO]:
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def _factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _upload(factory: sessionmaker[Session], staged: Path, payload: bytes, *, digest: str | None = None) -> str:
    with factory.begin() as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        dataset = models.Dataset(project_id=project.id, name="dataset")
        version = models.DatasetVersion(dataset_id=dataset.id, version_number=1, name="v1")
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
        run = models.TrainingRun(
            project_id=project.id,
            dataset_version_id=version.id,
            name="run",
            status="running",
            wandb_run_id="wandb-run",
        )
        session.add_all([workspace, project, dataset, version, source, credential, run])
        session.flush()
        launch = models.TrainingLaunch(
            workspace_id=workspace.id,
            project_id=project.id,
            run_id=run.id,
            dataset_version_id=version.id,
            wandb_credential_id=credential.id,
            client_request_id="request-1",
            source_id=source.id,
            run_prefix=f"projects/{project.id}/runs/{run.id}/",
        )
        upload = models.RunUpload(
            run_id=run.id,
            relative_path="media/images/sample.png",
            kind="image",
            object_key=f"{launch.run_prefix}samples/sample.png",
            metric_name="sample",
            step=12,
            caption="test sample",
            expected_sha256=digest or hashlib.sha256(payload).hexdigest(),
            expected_size=len(payload),
            mime_type="image/png",
            state="uploaded",
            metadata_={"staged_path": str(staged), "width": 2, "height": 2},
        )
        session.add_all([launch, upload])
        session.flush()
        return upload.id


def test_reconciler_promotes_and_verifies_sample_upload(tmp_path: Path) -> None:
    payload = b"sample-image"
    staged = tmp_path / "staged"
    staged.write_bytes(payload)
    factory = _factory()
    upload_id = _upload(factory, staged, payload)
    s3 = FakeS3()

    completed = WandbUploadReconciler(factory, client_factory=lambda _source: s3).run_once()

    assert completed == 1
    assert not staged.exists()
    with factory() as session:
        upload = session.get(models.RunUpload, upload_id)
        assert upload is not None
        assert upload.state == "verified"
        assert upload.version_id == "version-1"
        asset = session.get(models.Asset, upload.asset_id)
        assert asset is not None
        assert asset.sha256 == hashlib.sha256(payload).hexdigest()
        location = session.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == asset.id))
        assert location is not None
        assert location.uri == f"s3://training/{upload.object_key}"
        assert location.verification_state == "verified"
        sample = session.scalar(select(models.Sample).where(models.Sample.run_id == upload.run_id))
        assert sample is not None
        assert sample.asset_id == asset.id
        assert sample.step == 12
        assert sample.prompt == "test sample"
        assert sample.generation_metadata["upload_state"] == "verified"
        event_types = set(session.scalars(select(models.RunEvent.type).where(models.RunEvent.run_id == upload.run_id)))
        assert {"run.upload.verified", "run.sample.ready"} <= event_types



def test_reconciler_reuses_existing_source_location_for_sample(tmp_path: Path) -> None:
    payload = b"sample-image"
    staged = tmp_path / "staged"
    staged.write_bytes(payload)
    factory = _factory()
    upload_id = _upload(factory, staged, payload)
    with factory.begin() as session:
        upload = session.get(models.RunUpload, upload_id)
        assert upload is not None
        launch = session.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == upload.run_id))
        assert launch is not None
        source = session.get(models.ImportSource, launch.source_id)
        assert source is not None
        imported_asset = models.Asset(
            workspace_id=launch.workspace_id,
            project_id=launch.project_id,
            kind=models.AssetKind.image,
            name="sample.png",
            mime_type="image/png",
            metadata_={"category": "sample"},
        )
        session.add(imported_asset)
        session.flush()
        session.add(
            models.AssetLocation(
                asset_id=imported_asset.id,
                workspace_id=launch.workspace_id,
                provider="s3",
                uri=f"s3://{source.bucket}/{upload.object_key}",
                bucket=source.bucket,
                object_key=upload.object_key,
                etag=hashlib.md5(payload).hexdigest(),  # noqa: S324
                source_id=source.id,
                source_revision_fingerprint="imported",
                size=len(payload),
                hydration_state="remote",
            )
        )
        session.flush()
        imported_asset_id = imported_asset.id

    s3 = FakeS3()
    completed = WandbUploadReconciler(factory, client_factory=lambda _source: s3).run_once()

    assert completed == 1
    with factory() as session:
        upload = session.get(models.RunUpload, upload_id)
        assert upload is not None
        assert upload.asset_id == imported_asset_id
        locations = list(
            session.scalars(
                select(models.AssetLocation).where(
                    models.AssetLocation.object_key == upload.object_key
                )
            )
        )
        assert len(locations) == 1
        assert locations[0].asset_id == imported_asset_id
        location = session.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == imported_asset_id))
        assert location is not None
        assert location.verified_sha256 == hashlib.sha256(payload).hexdigest()

def test_reconciler_rejects_digest_mismatch_without_creating_sample(tmp_path: Path) -> None:
    payload = b"tampered-image"
    staged = tmp_path / "staged"
    staged.write_bytes(payload)
    factory = _factory()
    upload_id = _upload(factory, staged, payload, digest="0" * 64)
    s3 = FakeS3()

    completed = WandbUploadReconciler(factory, client_factory=lambda _source: s3).run_once()

    assert completed == 1
    with factory() as session:
        upload = session.get(models.RunUpload, upload_id)
        assert upload is not None
        assert upload.state == "failed"
        assert upload.error == "uploaded object digest does not match W&B metadata"
        assert upload.asset_id is None
        assert session.scalar(select(models.Sample)) is None
