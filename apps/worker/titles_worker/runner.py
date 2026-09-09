from __future__ import annotations

import logging
import os
import random
import signal
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from titles_api import __version__
from titles_api.models import GenerationQueueChild, GenerationQueueItem, ImportJob, Job, JobState, WorkerHeartbeat

logger = logging.getLogger("titles.worker")
JobHandler = Callable[["JobContext", dict[str, Any]], dict[str, Any] | None]
MaintenanceHandler = Callable[[], Any]


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
            queue_item = session.scalar(select(GenerationQueueItem).where(GenerationQueueItem.job_id == job.id))
            queue_child = session.scalar(select(GenerationQueueChild).where(GenerationQueueChild.job_id == job.id))
            if queue_child:
                queue_item = session.get(GenerationQueueItem, queue_child.queue_item_id)
                queue_child.state = "running"; queue_child.progress = {**dict(queue_child.progress or {}), "fraction": value}; queue_child.result = dict(job.result or {})
            if queue_item:
                queue_item.state = "running"
                children = list(session.scalars(select(GenerationQueueChild).where(GenerationQueueChild.queue_item_id == queue_item.id)))
                if children:
                    totals = [float((child.progress or {}).get("total", 1)) for child in children]
                    completed = [total if child.state == "completed" else float((child.progress or {}).get("fraction", 0)) * total for child, total in zip(children, totals)]
                    queue_item.progress = {"completed": sum(completed), "total": sum(totals), "fraction": sum(completed) / sum(totals)}
                else:
                    queue_item.progress = {"fraction": value}
                queue_item.result = dict(job.result or {})
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
        maintenance_handlers: tuple[MaintenanceHandler, ...] = (),
        maintenance_interval: float = 5.0,
        heartbeat_interval: float = 2.0,
    ):
        self.session_factory = session_factory
        self.handlers = handlers
        self.poll_interval = poll_interval
        self.max_attempts = max_attempts
        self.maintenance_handlers = maintenance_handlers
        self.maintenance_interval = maintenance_interval
        self.heartbeat_interval = heartbeat_interval
        self.worker_id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc)
        self._heartbeat_stop = threading.Event()
        self._next_maintenance_at = 0.0
        self._retry_backoff: dict[str, float] = {}
        self._stopping = False

    def _finish_safely(self, job: Job, state: JobState, **kwargs: Any) -> bool:
        try:
            self._finish(job.id, state, **kwargs)
            return True
        except Exception:
            logger.exception(
                "job finalization failed",
                extra={"job_id": job.id, "kind": job.kind, "state": state.value},
            )
            return False

    def recover_interrupted(self) -> int:
        """Single-host recovery: running jobs are safe to reclaim on process start."""
        with self.session_factory.begin() as session:
            running_ids = list(session.scalars(select(Job.id).where(Job.state == JobState.running)))
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
            session.execute(update(GenerationQueueItem).where(GenerationQueueItem.job_id.in_(running_ids)).values(state="queued", error="worker interrupted; resuming"))
            session.execute(update(GenerationQueueChild).where(GenerationQueueChild.job_id.in_(running_ids)).values(state="queued", error="worker interrupted; resuming"))
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
        # Materialize one DAM-owned generation item into the existing FAL job queue.
        # Provider-owned queues are excluded by the queue claim contract.
        with self.session_factory() as session:
            from titles_api.routers.generation_queue import claim_next_for_worker
            claim_next_for_worker(session)
        job = self.claim()
        if job is None:
            return False
        handler = self.handlers.get(job.kind)
        if handler is None:
            self._finish_safely(job, JobState.failed, error=f"no handler registered for job kind: {job.kind}")
            return True
        context = JobContext(self.session_factory, job.id)
        if job.cancel_requested:
            self._finish_safely(job, JobState.canceled)
            return True
        try:
            result = handler(context, dict(job.payload)) or {}
            state = JobState.canceled if context.cancellation_requested() else JobState.succeeded
            self._finish_safely(job, state, result=result, progress=1.0 if state == JobState.succeeded else job.progress)
        except RetryJob as exc:
            if job.attempts >= self.max_attempts:
                self._finish_safely(job, JobState.failed, error=str(exc))
                self._retry_backoff.pop(job.id, None)
            else:
                self._finish_safely(job, JobState.queued, error=str(exc))
                base = min(max(exc.delay_seconds, 0.25), 30.0)
                delay = min(self._retry_backoff.get(job.id, base), 30.0)
                self._retry_backoff[job.id] = min(delay * 2.0, 30.0)
                time.sleep(delay * random.uniform(0.8, 1.2))
        except Exception as exc:
            logger.exception("job failed", extra={"job_id": job.id, "kind": job.kind})
            self._finish_safely(job, JobState.failed, error=str(exc))
            self._retry_backoff.pop(job.id, None)
        return True

    def run_maintenance(self) -> bool:
        now = time.monotonic()
        if not self.maintenance_handlers or now < self._next_maintenance_at:
            return False
        self._next_maintenance_at = now + self.maintenance_interval
        for handler in self.maintenance_handlers:
            try:
                handler()
            except Exception:
                logger.exception("worker maintenance handler failed")
        return True

    def record_heartbeat(self, *, state: str = "running") -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory.begin() as session:
            heartbeat = session.get(WorkerHeartbeat, self.worker_id)
            if heartbeat is None:
                heartbeat = WorkerHeartbeat(
                    worker_id=self.worker_id,
                    state=state,
                    pid=os.getpid(),
                    version=__version__,
                    started_at=self.started_at,
                    heartbeat_at=now,
                )
                session.add(heartbeat)
            else:
                heartbeat.state = state
                heartbeat.heartbeat_at = now

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.is_set():
            try:
                self.record_heartbeat()
            except Exception:
                logger.exception("worker heartbeat failed")
            self._heartbeat_stop.wait(self.heartbeat_interval)


    def run_forever(self) -> None:
        recovered = self.recover_interrupted()
        if recovered:
            logger.info("recovered interrupted jobs", extra={"count": recovered})
        self._install_signal_handlers()
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="worker-heartbeat", daemon=True)
        heartbeat_thread.start()
        try:
            while not self._stopping:
                worked = self.run_one()
                self.run_maintenance()
                if not worked:
                    time.sleep(self.poll_interval)
        finally:
            self._heartbeat_stop.set()
            heartbeat_thread.join(timeout=max(self.heartbeat_interval * 2, 1))
            try:
                self.record_heartbeat(state="stopped")
            except Exception:
                logger.exception("final worker heartbeat failed")

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
            queue_item = session.scalar(select(GenerationQueueItem).where(GenerationQueueItem.job_id == job.id))
            queue_child = session.scalar(select(GenerationQueueChild).where(GenerationQueueChild.job_id == job.id))
            if queue_child:
                queue_item = session.get(GenerationQueueItem, queue_child.queue_item_id)
                queue_child.state = "completed" if state == JobState.succeeded else "queued" if state == JobState.queued else "failed"
                queue_child.progress = {**dict(queue_child.progress or {}), "fraction": progress}; queue_child.result = dict(result or job.result or {}); queue_child.error = error
            if queue_item:
                children = list(session.scalars(select(GenerationQueueChild).where(GenerationQueueChild.queue_item_id == queue_item.id).order_by(GenerationQueueChild.ordinal)))
                if children:
                    totals = [float((child.progress or {}).get("total", 1)) for child in children]
                    completed_children = sum(child.state == "completed" for child in children)
                    completed = sum(total if child.state == "completed" else 0 for child, total in zip(children, totals))
                    failed = [child for child in children if child.state == "failed"]
                    queue_item.state = "failed" if failed else "completed" if completed_children == len(children) else "queued"
                    queue_item.progress = {"completed": completed, "total": sum(totals), "fraction": completed / sum(totals)}
                    queue_item.result = dict(children[0].result or {}) if len(children) == 1 else {"children": [{"model_version_id": child.model_version_id, "status": child.state, "result": child.result} for child in children]}
                    queue_item.error = failed[0].error if failed else None
                    if queue_item.state == "queued":
                        queue_item.job_id = None; queue_item.provider_job_id = None
                else:
                    queue_item.state = "completed" if state == JobState.succeeded else "queued" if state == JobState.queued else "failed"
                    queue_item.progress = {"fraction": progress}; queue_item.result = dict(result or job.result or {}); queue_item.error = error
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
