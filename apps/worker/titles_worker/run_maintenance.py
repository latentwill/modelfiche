from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.training_metrics import append_run_event


class RunMaintenance:
    def __init__(self, session_factory: sessionmaker[Session], *, stale_after_seconds: int) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.session_factory = session_factory
        self.stale_after = timedelta(seconds=stale_after_seconds)

    def interrupt_stale_runs(self, *, limit: int = 100) -> int:
        now = models.utcnow()
        cutoff = now - self.stale_after
        with self.session_factory() as session:
            run_ids = list(
                session.scalars(
                    select(models.TrainingRun.id)
                    .where(
                        models.TrainingRun.status == "running",
                        or_(
                            models.TrainingRun.last_event_at < cutoff,
                            (
                                models.TrainingRun.last_event_at.is_(None)
                                & (models.TrainingRun.started_at < cutoff)
                            ),
                        ),
                    )
                    .order_by(models.TrainingRun.last_event_at, models.TrainingRun.started_at)
                    .limit(limit)
                )
            )
        interrupted = 0
        for run_id in run_ids:
            with self.session_factory.begin() as session:
                run = session.get(models.TrainingRun, run_id, with_for_update=True)
                if run is None or run.status != "running":
                    continue
                last_seen = run.last_event_at or run.started_at
                if last_seen is None or last_seen >= cutoff:
                    continue
                run.status = "interrupted"
                run.finished_at = now
                run.last_event_at = now
                launch = session.scalar(
                    select(models.TrainingLaunch).where(models.TrainingLaunch.run_id == run.id)
                )
                if launch is not None and launch.state not in {"completed", "failed"}:
                    launch.state = "interrupted"
                append_run_event(
                    session,
                    run,
                    type="run.interrupted",
                    idempotency_key=f"run:interrupted:stale:{run.id}",
                    payload={
                        "reason": "heartbeat_timeout",
                        "stale_after_seconds": int(self.stale_after.total_seconds()),
                        "last_seen_at": last_seen.isoformat(),
                    },
                    step=run.current_step,
                    occurred_at=now,
                )
                interrupted += 1
        return interrupted


__all__ = ["RunMaintenance"]
