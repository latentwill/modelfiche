import json
import io
import sqlite3


def _loss_log_bytes() -> bytes:
    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE steps (step INTEGER PRIMARY KEY, wall_time REAL NOT NULL);
        CREATE TABLE metrics (
            step INTEGER NOT NULL,
            key TEXT NOT NULL,
            value_real REAL,
            value_text TEXT,
            PRIMARY KEY (step, key)
        );
        INSERT INTO steps VALUES (1, 10.0), (2, 11.0);
        INSERT INTO metrics VALUES (1, 'loss', 1.5, NULL), (2, 'loss', 0.75, NULL);
        """
    )
    output = database.serialize()
    database.close()
    return output
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from titles_api import models
from titles_api.database import Base
from titles_api.integrations.s3.detection import DetectionResult, PrefixKind
from titles_api.integrations.s3.importer import ImportContext, ImportResult
from titles_api.integrations.s3.models import ObjectInfo
from titles_api.integrations.s3.sqlalchemy_sink import SQLAlchemyImportSink


def test_import_persists_loss_metrics_and_verifies_checksum_backed_remote_locations():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    prefix = "projects/title/runs/track-c/"
    image_key = prefix + "samples/step-2.png"
    checksum = "a" * 64
    with Session(engine) as session:
        workspace = models.Workspace(name="test")
        project = models.Project(workspace_id=workspace.id, title="title")
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="test",
            provider="s3",
            bucket="bucket",
            allowed_prefixes=["projects/"],
            addressing_style="auto",
            credential_env_prefix="S3",
        )
        session.add_all([workspace, project, source])
        session.flush()
        sink = SQLAlchemyImportSink(session)
        asset_id = sink.upsert_remote_object(
            source.id,
            ObjectInfo(key=image_key, size=123, etag="etag"),
            category="sample",
        )
        sink.save_source_snapshot(
            source.id,
            prefix + "checksums.sha256",
            f"{checksum}  {image_key}\n".encode(),
        )
        sink.save_source_snapshot(source.id, prefix + "loss_log.db", _loss_log_bytes())
        sink.save_source_snapshot(source.id, prefix + "train.log", b'{"step": 3, "loss": 0.25}\n')
        detection = DetectionResult(PrefixKind.TRAINING_RUN, 1.0, (), (), {"manifest": {}})
        sink.finalize_import(ImportContext(source.id, project.id, prefix), ImportResult(detection, 1, 1, 0, 0, 2, ()))

        asset = session.get(models.Asset, asset_id)
        location = session.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == asset_id))
        metrics = list(session.scalars(select(models.TrainingMetric).order_by(models.TrainingMetric.step)))

        assert asset is not None
        assert asset.content_blob_id is not None
        assert location is not None
        assert location.workspace_id == workspace.id
        assert location.source_id == source.id
        assert location.verification_state == "available"
        assert location.verified_sha256 == checksum
        assert [(metric.name, metric.step, metric.value) for metric in metrics] == [("loss", 1, 1.5), ("loss", 2, 0.75), ("loss", 3, 0.25)]
def test_overlapping_checkpoint_steps_are_preserved_per_stage():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    prefix = "projects/title/runs/track-b/"
    with Session(engine) as session:
        workspace = models.Workspace(name="test")
        session.add(workspace)
        session.flush()
        project = models.Project(workspace_id=workspace.id, title="title")
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="test",
            provider="s3",
            bucket="bucket",
            allowed_prefixes=["projects/"],
            addressing_style="auto",
            credential_env_prefix="S3",
        )
        session.add_all([project, source])
        session.flush()
        sink = SQLAlchemyImportSink(session)
        keys = [
            prefix + "flat/checkpoints/step-1000.safetensors",
            prefix + "render/checkpoints/step-1000.safetensors",
            prefix + "flat/samples/step-1000.png",
            prefix + "render/samples/step-1000.png",
        ]
        for key in keys:
            category = "checkpoint" if key.endswith(".safetensors") else "sample"
            asset = models.Asset(
                workspace_id=workspace.id,
                project_id=project.id,
                kind=models.AssetKind.model if category == "checkpoint" else models.AssetKind.image,
                name=key.rsplit("/", 1)[-1],
                metadata_={"category": category},
            )
            session.add(asset)
            session.flush()
            sink._assets[key] = asset.id
        detection = DetectionResult(PrefixKind.TRAINING_RUN, 1.0, (), (), {"checkpoints": 2, "samples": 2})
        result = ImportResult(detection, 4, 0, 0, 0, 0, ())
        sink.finalize_import(ImportContext(source.id, project.id, prefix), result)

        stages = {stage.id: stage.name for stage in session.scalars(select(models.TrainingStage))}
        checkpoints = list(session.scalars(select(models.Checkpoint)))
        samples = list(session.scalars(select(models.Sample)))
        assert set(stages.values()) == {"flat", "render"}
        assert len(checkpoints) == 2
        assert {(stages[item.stage_id], item.step) for item in checkpoints} == {("flat", 1000), ("render", 1000)}
        checkpoint_stage = {item.id: stages[item.stage_id] for item in checkpoints}
        assert {checkpoint_stage[item.checkpoint_id] for item in samples} == {"flat", "render"}


def test_dataset_refresh_populates_an_existing_empty_version_and_metadata():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    prefix = "datasets/portraits/"
    with Session(engine) as session:
        workspace = models.Workspace(name="test")
        project = models.Project(workspace_id=workspace.id, title="title")
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="test",
            provider="s3",
            bucket="bucket",
            allowed_prefixes=["datasets/"],
            addressing_style="auto",
            credential_env_prefix="S3",
        )
        session.add_all([workspace, project, source])
        session.flush()
        dataset = models.Dataset(project_id=project.id, name="portraits")
        session.add(dataset)
        session.flush()
        version = models.DatasetVersion(
            dataset_id=dataset.id,
            version_number=1,
            name="portraits",
            source_uri="s3://bucket/datasets/portraits",
        )
        session.add(version)
        session.flush()

        sink = SQLAlchemyImportSink(session)
        asset_id = sink.upsert_remote_object(
            source.id,
            ObjectInfo(key=prefix + "one.png", size=123, etag="etag"),
            category="dataset_image",
        )
        sink.save_source_snapshot(source.id, prefix + "one.txt", b"portrait caption")
        detection = DetectionResult(PrefixKind.DATASET, 1.0, (), (), {"images": 1})
        sink.finalize_import(ImportContext(source.id, project.id, prefix), ImportResult(detection, 1, 0, 0, 0, 1, ()))

        items = list(session.scalars(select(models.DatasetItem)))
        asset = session.get(models.Asset, asset_id)
        assert len(items) == 1
        assert items[0].dataset_version_id == version.id
        assert items[0].caption == "portrait caption"
        assert asset.metadata_["caption"] == "portrait caption"
        assert asset.metadata_["dataset_version_id"] == version.id

        json_prefix = "datasets/structured/"
        json_sink = SQLAlchemyImportSink(session)
        json_asset_id = json_sink.upsert_remote_object(
            source.id,
            ObjectInfo(key=json_prefix + "one.png", size=321, etag="json-etag"),
            category="dataset_image",
        )
        json_sink.save_source_snapshot(
            source.id,
            json_prefix + "one.txt",
            b'{"description":"An orange lighthouse","objects":["lighthouse","sea"]}',
        )
        json_sink.finalize_import(
            ImportContext(source.id, project.id, json_prefix),
            ImportResult(detection, 1, 0, 0, 0, 1, ()),
        )
        json_version = session.scalar(select(models.DatasetVersion).where(models.DatasetVersion.source_uri == "s3://bucket/datasets/structured"))
        json_item = session.scalar(select(models.DatasetItem).where(models.DatasetItem.dataset_version_id == json_version.id))
        json_asset = session.get(models.Asset, json_asset_id)

        assert json_version.caption_format == "json"
        assert json_item.caption_format == "json"
        assert json.loads(json_item.caption)["description"] == "An orange lighthouse"
        assert json_asset.metadata_["caption_format"] == "json"
