from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.database import Base
from titles_worker.sqlalchemy_eval_sink import SQLAlchemyEvalOutputSink


def test_ingest_outputs_preserves_prompt_and_fal_metadata(tmp_path: Path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        prompt_set = models.PromptSet(project_id=project.id, name="prompts")
        session.add_all([workspace, project, prompt_set])
        session.flush()
        prompt = models.Prompt(prompt_set_id=prompt_set.id, text="a cinematic portrait", position=0)
        definition = models.EvalDefinition(
            project_id=project.id,
            name="eval",
            endpoint="fal-ai/test-model",
            prompt_set_id=prompt_set.id,
            parameters={"guidance_scale": 3.5},
        )
        session.add_all([prompt, definition])
        session.flush()
        run = models.EvalRun(
            definition_id=definition.id,
            status="running",
            provider_job_ids=["request-123"],
            parameters_snapshot={"guidance_scale": 4.0},
        )
        session.add(run)

    local = tmp_path / "fal-output.png"
    local.write_bytes(b"image")
    sink = SQLAlchemyEvalOutputSink(factory, cache=object())
    sink._download = lambda _url, _size: SimpleNamespace(path=local, size=5, sha256="a" * 64)
    result = sink.ingest_outputs(
        run.id,
        prompt.id,
        {
            "seed": 17,
            "timings": {"inference": 1.2},
            "images": [{"url": "https://fal.media/files/output.png", "content_type": "image/png", "width": 640, "height": 480, "file_size": 5}],
        },
    )

    assert result["outputs"] == 1
    with Session(engine) as session:
        output = session.scalar(select(models.EvalOutput))
        asset = session.get(models.Asset, output.asset_id)
        assert asset.project_id == project.id
        assert asset.metadata_["caption"] == "a cinematic portrait"
        assert asset.metadata_["parameters"] == {"guidance_scale": 4.0}
        assert output.provider_metadata["source_url"] == "https://fal.media/files/output.png"
        assert output.provider_metadata["request_ids"] == ["request-123"]
        assert output.provider_metadata["response"]["timings"] == {"inference": 1.2}
        location = session.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == asset.id))
        assert location.workspace_id == workspace.id
        assert location.verification_state == "available"
        assert location.verified_sha256 == "a" * 64
        assert asset.content_blob_id is not None
        assert asset.preferred_location_id == location.id
