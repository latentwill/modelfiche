from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.asset_cache import AssetCache
from titles_api.integrations.config import CacheSettings
from titles_api.integrations.s3.browser import PrefixAccessError
from titles_api.database import Base
from titles_worker.image_lineage_jobs import ImageLineageRepairHandler
from titles_worker.sqlalchemy_eval_sink import SQLAlchemyEvalOutputSink


def test_ingest_outputs_preserves_prompt_and_fal_metadata(tmp_path: Path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        prompt_set = models.PromptSet(project_id=project.id, name="prompts")
        session.add_all([workspace, project, prompt_set])
        session.flush()
        prompt = models.Prompt(
            prompt_set_id=prompt_set.id, text="a cinematic portrait", position=0
        )
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
    durable_root = tmp_path / "assets"
    monkeypatch.setattr(
        "titles_worker.sqlalchemy_eval_sink.get_settings",
        lambda: SimpleNamespace(asset_root=durable_root),
    )
    cache = AssetCache(CacheSettings(root=tmp_path / "cache", max_bytes=512 * 1024**2))
    sink = SQLAlchemyEvalOutputSink(factory, cache=cache)
    sink._download = lambda _url, _size: SimpleNamespace(
        path=local, size=5, sha256="a" * 64
    )
    result = sink.ingest_outputs(
        run.id,
        prompt.id,
        {
            "seed": 17,
            "generated_at": "2026-03-01T00:00:00Z",
            "_history": {
                "sent_at": "2026-04-01T00:00:00Z",
                "ended_at": "2026-05-01T12:30:00Z",
            },
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
            "images": [
                {
                    "url": "https://fal.media/files/output.png",
                    "content_type": "image/png",
                    "width": 640,
                    "height": 480,
                    "file_size": 5,
                }
            ],
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
        assert (
            output.provider_metadata["source_url"]
            == "https://fal.media/files/output.png"
        )
        assert output.provider_metadata["request_ids"] == ["request-123"]
        assert output.provider_metadata["_request_input"] == {"lora_scale": 0.75}
        assert output.provider_metadata["response"]["timings"] == {"inference": 1.2}
        assert output.generated_at == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
        location = session.scalar(
            select(models.AssetLocation).where(
                models.AssetLocation.asset_id == asset.id
            )
        )
        assert location.workspace_id == workspace.id
        assert location.verification_state == "available"
        assert location.verified_sha256 == "a" * 64
        assert asset.content_blob_id is not None
        assert Path(location.uri).is_relative_to(durable_root)
        local.unlink()
        assert Path(location.uri).read_bytes() == b"image"
        assert asset.preferred_location_id == location.id
        activity = session.scalar(
            select(models.ActivityEvent).where(
                models.ActivityEvent.subject_id == asset.id
            )
        )
        assert activity.action == "asset.generated"
        assert activity.workspace_id == workspace.id
        assert activity.project_id == project.id
        assert activity.details["endpoint"] == "fal-ai/test-model"
        assert "summary" not in activity.details


def test_image_lineage_repair_moves_recorded_sources_out_of_cache(
    tmp_path: Path, monkeypatch
):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    fal_body = b"fal-image"
    s3_body = b"s3-image"
    cache_root = tmp_path / "cache"
    asset_root = tmp_path / "assets"
    cache_root.mkdir()
    asset_root.mkdir()
    monkeypatch.setattr(
        "titles_worker.image_lineage_jobs.get_settings",
        lambda: SimpleNamespace(asset_root=asset_root, cache_root=cache_root),
    )
    with factory.begin() as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        definition = models.EvalDefinition(project_id=project.id, name="eval")
        session.add_all([workspace, project, definition])
        session.flush()
        run = models.EvalRun(definition_id=definition.id)
        fal_asset = models.Asset(
            workspace_id=workspace.id,
            project_id=project.id,
            kind=models.AssetKind.image,
            name="fal.jpg",
            sha256=hashlib.sha256(fal_body).hexdigest(),
        )
        s3_asset = models.Asset(
            workspace_id=workspace.id,
            project_id=project.id,
            kind=models.AssetKind.image,
            name="s3.jpg",
            sha256=hashlib.sha256(s3_body).hexdigest(),
        )
        orphan_asset = models.Asset(
            workspace_id=workspace.id,
            project_id=project.id,
            kind=models.AssetKind.image,
            name="orphan.jpg",
            sha256="f" * 64,
        )
        source = models.ImportSource(
            workspace_id=workspace.id,
            name="disconnected archive",
            provider="s3",
            bucket="archive",
            allowed_prefixes=["images/"],
            credential_env_prefix="ARCHIVE",
            is_active=False,
        )
        session.add_all([run, fal_asset, s3_asset, orphan_asset, source])
        session.flush()
        fal_local = models.AssetLocation(
            asset_id=fal_asset.id,
            workspace_id=workspace.id,
            provider="local",
            uri=str(cache_root / "objects" / "missing-fal"),
            size=len(fal_body),
            verified_size=len(fal_body),
            verified_sha256=fal_asset.sha256,
            verification_state="available",
            hydration_state="hydrated",
        )
        s3_local = models.AssetLocation(
            asset_id=s3_asset.id,
            workspace_id=workspace.id,
            provider="local",
            uri=str(cache_root / "objects" / "missing-s3"),
            size=len(s3_body),
            verified_size=len(s3_body),
            verified_sha256=s3_asset.sha256,
            verification_state="available",
            hydration_state="hydrated",
        )
        orphan_local = models.AssetLocation(
            asset_id=orphan_asset.id,
            workspace_id=workspace.id,
            provider="local",
            uri=str(cache_root / "objects" / "missing-orphan"),
            size=9,
            verified_size=9,
            verified_sha256=orphan_asset.sha256,
            verification_state="available",
            hydration_state="hydrated",
        )
        remote = models.AssetLocation(
            asset_id=s3_asset.id,
            workspace_id=workspace.id,
            provider="s3",
            uri="s3://archive/images/s3.jpg",
            bucket="archive",
            object_key="images/s3.jpg",
            source_id=source.id,
            size=len(s3_body),
            verified_size=len(s3_body),
            verified_sha256=s3_asset.sha256,
            verification_state="unverified",
            hydration_state="remote",
        )
        session.add_all([fal_local, s3_local, orphan_local, remote])
        session.flush()
        orphan_asset.preferred_location_id = orphan_local.id
        session.add(
            models.EvalOutput(
                eval_run_id=run.id,
                asset_id=fal_asset.id,
                provider_metadata={"source_url": "https://fal.media/fal.jpg"},
            )
        )
        fal_asset_id = fal_asset.id
        s3_asset_id = s3_asset.id
        fal_location_id = fal_local.id
        s3_location_id = s3_local.id
        orphan_asset_id = orphan_asset.id
        orphan_location_id = orphan_local.id

    class FakeS3Client:
        @staticmethod
        def get_object(**request):
            assert request == {"Bucket": "archive", "Key": "images/s3.jpg"}
            return {"Body": io.BytesIO(s3_body)}

    class FakeBrowser:
        client = FakeS3Client()

        @staticmethod
        def download(key: str, stream, *, etag=None) -> None:
            assert key == "images/s3.jpg"
            raise PrefixAccessError("recorded key is outside the current browse scope")

    transport = SimpleNamespace(
        request=lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200, body=fal_body
        )
    )
    context = SimpleNamespace(
        cancellation_requested=lambda: False,
        progress=lambda _value, **_kwargs: None,
    )
    handler = ImageLineageRepairHandler(
        factory,
        AssetCache(CacheSettings(root=cache_root, max_bytes=512 * 1024**2)),
        artifact_transport=transport,
        s3_browser_factory=lambda _settings: FakeBrowser(),
    )

    result = handler(context, {"project_id": project.id})

    assert result == {
        "repaired": 2,
        "skipped": 0,
        "failed": 1,
        "failures": [{"asset_id": orphan_asset_id, "error": "LookupError"}],
        "canceled": False,
    }
    with Session(engine) as session:
        source = session.get(models.ImportSource, source.id)
        assert source.is_active is False
        for asset_id, location_id, body in (
            (fal_asset_id, fal_location_id, fal_body),
            (s3_asset_id, s3_location_id, s3_body),
        ):
            asset = session.get(models.Asset, asset_id)
            location = session.get(models.AssetLocation, location_id)
            path = Path(location.uri)
            assert path.is_relative_to(asset_root)
            assert path.read_bytes() == body
            assert location.repair_attribution == "image_lineage.durable_source_repair"
            assert asset.preferred_location_id == location.id
            assert asset.content_blob_id is not None
        orphan_asset = session.get(models.Asset, orphan_asset_id)
        orphan_location = session.get(models.AssetLocation, orphan_location_id)
        assert orphan_asset.preferred_location_id is None
        assert orphan_location.verification_state == "unavailable"
        assert orphan_location.hydration_state == "missing"
        assert orphan_location.repair_attribution == "image_lineage.source_unavailable"
