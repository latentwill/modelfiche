from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from titles_api.models import ImportJob, Job, JobState

logger = logging.getLogger("titles.worker")
JobHandler = Callable[["JobContext", dict[str, Any]], dict[str, Any] | None]


class RetryJob(RuntimeError):
    def __init__(self, message: str, *, delay_seconds: float = 2.0):
        super().__init__(message)
        self.delay_seconds = delay_seconds


@dataclass(slots=True)
class JobContext:
    session_factory: sessionmaker[Session]
    job_id: str

    def progress(self, value: float, *, result_patch: dict[str, Any] | None = None) -> None:
        value = min(max(float(value), 0.0), 1.0)
        with self.session_factory.begin() as session:
            job = session.get(Job, self.job_id)
            if not job:
                raise LookupError(self.job_id)
            job.progress = value
            if result_patch:
                job.result = {**job.result, **result_patch}
            import_job = session.scalar(select(ImportJob).where(ImportJob.job_id == job.id))
            if import_job:
                import_job.state = JobState.running.value
                import_job.progress = value

    def checkpoint(self, **payload_patch: Any) -> None:
        """Persist provider IDs/state so a restarted handler can resume safely."""
        with self.session_factory.begin() as session:
            job = session.get(Job, self.job_id)
            if not job:
                raise LookupError(self.job_id)
            job.payload = {**job.payload, **payload_patch}

    def cancellation_requested(self) -> bool:
        with self.session_factory() as session:
            return bool(session.scalar(select(Job.cancel_requested).where(Job.id == self.job_id)))


class JobRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: dict[str, JobHandler],
        *,
        poll_interval: float = 0.75,
        max_attempts: int = 5,
    ):
        self.session_factory = session_factory
        self.handlers = handlers
        self.poll_interval = poll_interval
        self.max_attempts = max_attempts
        self._stopping = False

    def recover_interrupted(self) -> int:
        """Single-host recovery: running jobs are safe to reclaim on process start."""
        with self.session_factory.begin() as session:
            running_ids = select(Job.id).where(Job.state == JobState.running)
            session.execute(
                update(ImportJob)
                .where(ImportJob.job_id.in_(running_ids))
                .values(state=JobState.queued.value)
            )
            result = session.execute(
                update(Job)
                .where(Job.state == JobState.running)
                .values(state=JobState.queued, error="worker interrupted; resuming")
            )
            return int(result.rowcount or 0)

    def claim(self) -> Job | None:
        with self.session_factory.begin() as session:
            candidate = session.scalar(
                select(Job).where(Job.state == JobState.queued).order_by(Job.created_at, Job.id).limit(1)
            )
            if candidate is None:
                return None
            claimed = session.execute(
                update(Job)
                .where(Job.id == candidate.id, Job.state == JobState.queued)
                .values(state=JobState.running, attempts=Job.attempts + 1, error=None)
            )
            if claimed.rowcount != 1:
                return None
            session.refresh(candidate)
            import_job = session.scalar(select(ImportJob).where(ImportJob.job_id == candidate.id))
            if import_job:
                import_job.state = JobState.running.value
                import_job.progress = candidate.progress
            session.expunge(candidate)
            return candidate

    def run_one(self) -> bool:
        job = self.claim()
        if job is None:
            return False
        handler = self.handlers.get(job.kind)
        if handler is None:
            self._finish(job.id, JobState.failed, error=f"no handler registered for job kind: {job.kind}")
            return True
        context = JobContext(self.session_factory, job.id)
        if job.cancel_requested:
            self._finish(job.id, JobState.canceled)
            return True
        try:
            result = handler(context, dict(job.payload)) or {}
            state = JobState.canceled if context.cancellation_requested() else JobState.succeeded
            self._finish(job.id, state, result=result, progress=1.0 if state == JobState.succeeded else job.progress)
        except RetryJob as exc:
            if job.attempts >= self.max_attempts:
                self._finish(job.id, JobState.failed, error=str(exc))
            else:
                self._finish(job.id, JobState.queued, error=str(exc))
                time.sleep(min(max(exc.delay_seconds, 0), 30))
        except Exception as exc:
            logger.exception("job failed", extra={"job_id": job.id, "kind": job.kind})
            self._finish(job.id, JobState.failed, error=str(exc))
        return True

    def run_forever(self) -> None:
        recovered = self.recover_interrupted()
        if recovered:
            logger.info("recovered interrupted jobs", extra={"count": recovered})
        self._install_signal_handlers()
        while not self._stopping:
            if not self.run_one():
                time.sleep(self.poll_interval)

    def stop(self) -> None:
        self._stopping = True

    def _finish(
        self,
        job_id: str,
        state: JobState,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        progress: float | None = None,
    ) -> None:
        with self.session_factory.begin() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise LookupError(job_id)
            job.state = state
            job.error = error
            if result is not None:
                job.result = result
            if progress is not None:
                job.progress = progress
            import_job = session.scalar(select(ImportJob).where(ImportJob.job_id == job_id))
            if import_job:
                import_job.state = state.value
                import_job.progress = job.progress
                if result is not None:
                    import_job.result = result
                elif error:
                    import_job.result = {**import_job.result, "error": error}
                if error:
                    import_job.warnings = [*import_job.warnings, {"level": "error", "message": error}]

    def _install_signal_handlers(self) -> None:
        def request_stop(_signum: int, _frame: Any) -> None:
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, request_stop)
            except ValueError:
                pass
