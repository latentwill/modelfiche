from datetime import datetime, timezone
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
            "generated_at": "2026-03-01T00:00:00Z",
            "_history": {"sent_at": "2026-04-01T00:00:00Z", "ended_at": "2026-05-01T12:30:00Z"},
            "_grid_metadata": {
                "parameters": {"lora_scale": 0.75},
                "grid_cell_id": "cell-1",
                "x_index": 2,
                "y_index": 1,
                "checkpoint_name": "step 750",
                "run_id": "training-run-1",
                "run_name": "portrait-v1",
                "base_model": "krea/Krea-2-Raw",
                "model_name": "Portrait",
                "model_version_name": "portrait-v001-step-750",
            },
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
        assert asset.metadata_["parameters"] == {"lora_scale": 0.75}
        assert asset.metadata_["fal_request_input"] == {"lora_scale": 0.75}
        assert asset.metadata_["grid_cell_id"] == "cell-1"
        assert asset.metadata_["checkpoint_name"] == "step 750"
        assert asset.metadata_["run_id"] == "training-run-1"
        assert asset.metadata_["run_name"] == "portrait-v1"
        assert asset.metadata_["base_model"] == "krea/Krea-2-Raw"
        assert asset.metadata_["model_name"] == "Portrait"
        assert asset.metadata_["model_version_name"] == "portrait-v001-step-750"
        assert output.provider_metadata["source_url"] == "https://fal.media/files/output.png"
        assert output.provider_metadata["request_ids"] == ["request-123"]
        assert output.provider_metadata["_request_input"] == {"lora_scale": 0.75}
        assert output.provider_metadata["response"]["timings"] == {"inference": 1.2}
        assert output.generated_at == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
        location = session.scalar(select(models.AssetLocation).where(models.AssetLocation.asset_id == asset.id))
        assert location.workspace_id == workspace.id
        assert location.verification_state == "available"
        assert location.verified_sha256 == "a" * 64
        assert asset.content_blob_id is not None
        assert asset.preferred_location_id == location.id
        activity = session.scalar(select(models.ActivityEvent).where(models.ActivityEvent.subject_id == asset.id))
        assert activity.action == "asset.generated"
        assert activity.workspace_id == workspace.id
        assert activity.project_id == project.id
        assert activity.details["endpoint"] == "fal-ai/test-model"
