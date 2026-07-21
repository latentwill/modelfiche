from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from titles_api.database import Base
from titles_api.models import WorkerHeartbeat
from titles_worker.runner import JobRunner


def test_worker_records_authoritative_lifecycle_heartbeat() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    runner = JobRunner(sessions, {})

    runner.record_heartbeat()
    with sessions() as session:
        heartbeat = session.get(WorkerHeartbeat, runner.worker_id)
        assert heartbeat is not None
        assert heartbeat.state == "running"
        assert heartbeat.pid > 0
        first = heartbeat.heartbeat_at

    runner.record_heartbeat(state="stopped")
    with sessions() as session:
        heartbeat = session.get(WorkerHeartbeat, runner.worker_id)
        assert heartbeat is not None
        assert heartbeat.state == "stopped"
        assert heartbeat.heartbeat_at >= first
