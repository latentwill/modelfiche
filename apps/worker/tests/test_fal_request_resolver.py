from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from titles_api import models
from titles_api.database import Base
from titles_worker.fal_request_resolver import SQLAlchemyFalRequestResolver


def _resolver(readiness: dict):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        workspace = models.Workspace(name="workspace")
        session.add(workspace); session.flush()
        project = models.Project(workspace_id=workspace.id, title="Modern Times")
        session.add(project); session.flush()
        run = models.TrainingRun(project_id=project.id, name="run")
        asset = models.Asset(workspace_id=workspace.id, project_id=project.id, kind=models.AssetKind.model, name="artifact.safetensors")
        model = models.Model(project_id=project.id, name="merged")
        session.add_all([run, asset, model]); session.flush()
        checkpoint = models.Checkpoint(run_id=run.id, step=3000, asset_id=asset.id)
        session.add(checkpoint); session.flush()
        session.add(models.ModelVersion(model_id=model.id, checkpoint_id=checkpoint.id, name="merged-3000", readiness=readiness))
        session.flush()
        checkpoint_id = checkpoint.id
    return SQLAlchemyFalRequestResolver(factory), checkpoint_id


def test_explicit_model_version_fal_url_precedes_missing_handoff_and_s3():
    resolver, checkpoint_id = _resolver({"fal_url": "https://v3b.fal.media/files/model.safetensors"})
    request = resolver({
        "checkpoint_id": checkpoint_id,
        "endpoint_id": "fal-ai/krea-2/turbo/lora",
        "prompt": "portrait",
        "parameters": {"endpoint_id": "fal-ai/krea-2/turbo/lora", "lora_scale": 1.0},
    })
    assert request["loras"][0]["path"] == "https://v3b.fal.media/files/model.safetensors"
    assert "endpoint_id" not in request


def test_explicit_model_version_fal_url_must_be_public_https():
    resolver, checkpoint_id = _resolver({"fal_url": "http://example.test/model.safetensors"})
    try:
        resolver({"checkpoint_id": checkpoint_id, "endpoint_id": "fal-ai/krea-2/turbo/lora", "prompt": "portrait", "parameters": {}})
    except ValueError as exc:
        assert "public HTTPS" in str(exc)
    else:
        raise AssertionError("expected unsafe FAL URL to fail")
