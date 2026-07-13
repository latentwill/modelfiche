from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from titles_api import models
from titles_api.database import Base
from titles_api import wandb_uploads


class FakeS3:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate_presigned_url(self, operation: str, *, Params: dict[str, object], ExpiresIn: int) -> str:
        self.calls.append({"operation": operation, "params": Params, "expires": ExpiresIn})
        return "https://objects.example.invalid/signed-upload?signature=secret"


def _rows(session: Session) -> tuple[models.TrainingRun, models.TrainingLaunch]:
    workspace = models.Workspace(name="workspace")
    project = models.Project(workspace_id=workspace.id, title="project")
    source = models.ImportSource(
        workspace_id=workspace.id,
        name="training",
        provider="s3",
        endpoint_url="https://objects.example.invalid",
        bucket="training",
        allowed_prefixes=["projects/"],
        credential_env_prefix="TRAINING_S3",
    )
    run = models.TrainingRun(project_id=project.id, name="run", wandb_run_id="wandb-run")
    session.add_all([workspace, project, source, run])
    session.flush()
    launch = models.TrainingLaunch(
        workspace_id=workspace.id,
        project_id=project.id,
        run_id=run.id,
        dataset_version_id="dataset-version",
        wandb_credential_id="credential",
        client_request_id="request",
        source_id=source.id,
        run_prefix=f"projects/{project.id}/runs/{run.id}/",
    )
    session.add(launch)
    session.flush()
    return run, launch


def test_upload_instruction_prefers_s3_without_persisting_signed_url(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    fake = FakeS3()
    monkeypatch.setattr(wandb_uploads, "create_source_s3_client", lambda _settings: fake)
    with Session(engine) as session:
        run, launch = _rows(session)

        upload, url = wandb_uploads.upload_instruction(
            session,
            run,
            launch,
            "media/images/sample.png",
            fallback_base_url="https://dam.example.com",
        )

        assert url == "https://objects.example.invalid/signed-upload?signature=secret"
        assert fake.calls == [
            {
                "operation": "put_object",
                "params": {"Bucket": "training", "Key": f"{launch.run_prefix}samples/sample.png"},
                "expires": 600,
            }
        ]
        assert upload.state == "pending"
        assert upload.presign_expires_at is not None
        assert upload.metadata_ == {"protocol": "wandb-0.28.0", "upload_mode": "s3"}
        assert "signature" not in str(upload.metadata_)


def test_upload_instruction_falls_back_to_generation_fenced_staging(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def unavailable(_settings):
        raise ValueError("credentials unavailable")

    monkeypatch.setattr(wandb_uploads, "create_source_s3_client", unavailable)
    with Session(engine) as session:
        run, launch = _rows(session)
        upload, first_url = wandb_uploads.upload_instruction(
            session,
            run,
            launch,
            "wandb-summary.json",
            fallback_base_url="https://dam.example.com",
        )
        upload.state = "failed"
        upload.error = "interrupted"
        refreshed, second_url = wandb_uploads.upload_instruction(
            session,
            run,
            launch,
            "wandb-summary.json",
            fallback_base_url="https://dam.example.com",
        )

        assert refreshed.id == upload.id
        assert refreshed.generation == 1
        assert refreshed.state == "pending"
        assert first_url.endswith(f"/wandb-upload/{upload.id}?generation=0")
        assert second_url.endswith(f"/wandb-upload/{upload.id}?generation=1")
        assert refreshed.metadata_["upload_mode"] == "staged"
