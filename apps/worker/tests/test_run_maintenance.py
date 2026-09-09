from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.database import Base
from titles_worker.run_maintenance import RunMaintenance


def test_stale_running_runs_become_interrupted_once() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    old = models.utcnow() - timedelta(minutes=20)
    with factory.begin() as session:
        workspace = models.Workspace(name="workspace")
        project = models.Project(workspace_id=workspace.id, title="project")
        stale = models.TrainingRun(
            project_id=project.id,
            name="stale",
            status="running",
            started_at=old,
            last_event_at=old,
        )
        fresh = models.TrainingRun(
            project_id=project.id,
            name="fresh",
            status="running",
            started_at=models.utcnow(),
            last_event_at=models.utcnow(),
        )
        session.add_all([workspace, project, stale, fresh])
        session.flush()
        launch = models.TrainingLaunch(
            workspace_id=workspace.id,
            project_id=project.id,
            run_id=stale.id,
            dataset_version_id="dataset-version",
            wandb_credential_id="credential",
            state="running",
            client_request_id="request",
            run_prefix=f"projects/{project.id}/runs/{stale.id}/",
        )
        session.add(launch)
        stale_id = stale.id
        fresh_id = fresh.id

    maintenance = RunMaintenance(factory, stale_after_seconds=900)
    assert maintenance.interrupt_stale_runs() == 1
    assert maintenance.interrupt_stale_runs() == 0

    with Session(engine) as session:
        stale = session.get(models.TrainingRun, stale_id)
        fresh = session.get(models.TrainingRun, fresh_id)
        assert stale is not None and stale.status == "interrupted"
        assert stale.finished_at is not None
        assert fresh is not None and fresh.status == "running"
        launch = session.scalar(select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == stale_id))
        assert launch is not None and launch.state == "interrupted"
        events = session.scalars(select(models.RunEvent).where(models.RunEvent.run_id == stale_id)).all()
        assert [(event.type, event.payload["reason"]) for event in events] == [
            ("run.interrupted", "heartbeat_timeout")
        ]
