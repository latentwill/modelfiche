from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from titles_api import models
from titles_api.database import Base
from titles_api.integrations.s3.detection import DetectionResult, PrefixKind
from titles_api.integrations.s3.importer import ImportContext, ImportResult
from titles_api.integrations.s3.sqlalchemy_sink import SQLAlchemyImportSink


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
