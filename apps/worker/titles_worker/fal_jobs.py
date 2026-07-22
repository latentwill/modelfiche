from __future__ import annotations

import time
from itertools import product
from types import SimpleNamespace
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.integrations.fal.client import FalProviderError, FalQueueClient

from .runner import JobContext, RetryJob


class EvalOutputSink(Protocol):
    def provider_submitted(self, eval_run_id: str, request_id: str, grid_cell_id: str | None = None) -> None: ...

    def ingest_outputs(self, eval_run_id: str, prompt_id: str | None, response: dict[str, Any]) -> dict[str, Any]: ...

    def mark_failed(self, eval_run_id: str, message: str) -> None: ...

    def mark_succeeded(self, eval_run_id: str) -> None: ...

    def mark_canceled(self, eval_run_id: str) -> None: ...
    def link_grid_cell(self, grid_definition_id: str, output_id: str, x_index: int, y_index: int, grid_cell_id: str | None = None) -> None: ...

    def grid_cell_exists(self, grid_definition_id: str, x_index: int, y_index: int, grid_cell_id: str | None = None) -> bool: ...


class RequestResolver(Protocol):
    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class FalEvalHandler:
    """Restart-safe submit/poll/ingest handler for one eval or grid cell."""

    TERMINAL_SUCCESS = {"COMPLETED"}
    TERMINAL_FAILURE = {"FAILED", "CANCELLED", "CANCELED"}

    def __init__(
        self,
        client: FalQueueClient,
        sink: EvalOutputSink,
        *,
        request_resolver: RequestResolver | None = None,
        poll_seconds: float = 1.5,
    ):
        self.client = client
        self.sink = sink
        self.request_resolver = request_resolver
        self.poll_seconds = poll_seconds

    def __call__(self, context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        eval_run_id = str(payload["eval_run_id"])
        try:
            if payload.get("grid_definition_id") and self.sink.grid_cell_exists(
                str(payload["grid_definition_id"]), int(payload["x_index"]), int(payload["y_index"])
            ):
                return {"already_ingested": True}
            if not payload.get("provider_request_id"):
                request = payload.get("request")
                if request is None and self.request_resolver:
                    request = self.request_resolver(payload)
                if request is None:
                    raise ValueError("FAL job requires request or a configured request resolver")
                intent = _submission_intent(
                    self.sink,
                    eval_run_id,
                    prompt_id=str(payload["prompt_id"]) if payload.get("prompt_id") is not None else None,
                    grid_cell_id=str(payload["grid_cell_id"]) if payload.get("grid_cell_id") is not None else None,
                )
                request_id = intent.get("provider_request_id") if intent else None
                if request_id:
                    resume = getattr(self.client, "resume_submission", None)
                    if resume is None:
                        raise RuntimeError("FAL submission requires reconciliation before polling")
                    submission = resume(str(payload["endpoint_id"]), str(request_id))
                else:
                    submission = self.client.submit(str(payload["endpoint_id"]), dict(request))
                    if intent:
                        record = getattr(self.sink, "record_submission_intent", None)
                        if record is None:
                            raise RuntimeError("FAL submission intent cannot be recorded")
                        record(str(intent["intent_id"]), submission.request_id)
                context.checkpoint(
                    provider_request_id=submission.request_id,
                    provider_status_url=submission.status_url,
                    provider_response_url=submission.response_url,
                    provider_cancel_url=submission.cancel_url,
                )
                self.sink.provider_submitted(eval_run_id, submission.request_id)
                payload.update(
                    provider_request_id=submission.request_id,
                    provider_status_url=submission.status_url,
                    provider_response_url=submission.response_url,
                    provider_cancel_url=submission.cancel_url,
                )
            while True:
                if context.cancellation_requested():
                    if payload.get("provider_cancel_url"):
                        self.client.cancel(str(payload["provider_cancel_url"]))
                    _finish_submission(
                        self.sink,
                        eval_run_id,
                        prompt_id=str(payload["prompt_id"]) if payload.get("prompt_id") is not None else None,
                        grid_cell_id=str(payload["grid_cell_id"]) if payload.get("grid_cell_id") is not None else None,
                        state="canceled",
                    )
                    self.sink.mark_canceled(eval_run_id)
                    return {"provider_request_id": payload["provider_request_id"], "canceled": True}
                status = self.client.status(str(payload["provider_status_url"]))
                provider_state = str(status.get("status", "")).upper()
                context.progress(_progress(status), result_patch={"provider_status": provider_state})
                if provider_state in self.TERMINAL_SUCCESS:
                    response = self.client.result(str(payload["provider_response_url"]))
                    response.setdefault("_request_id", str(payload["provider_request_id"]))
                    ingested = self.sink.ingest_outputs(eval_run_id, payload.get("prompt_id"), response)
                    if payload.get("grid_definition_id"):
                        for output_id in ingested.get("output_ids", []):
                            self.sink.link_grid_cell(
                                str(payload["grid_definition_id"]),
                                str(output_id),
                                int(payload["x_index"]),
                                int(payload["y_index"]),
                            )
                    _finish_submission(
                        self.sink,
                        eval_run_id,
                        prompt_id=str(payload["prompt_id"]) if payload.get("prompt_id") is not None else None,
                        grid_cell_id=str(payload["grid_cell_id"]) if payload.get("grid_cell_id") is not None else None,
                    )
                    self.sink.mark_succeeded(eval_run_id)
                    return {"provider_request_id": payload["provider_request_id"], **ingested}
                if provider_state in self.TERMINAL_FAILURE:
                    message = str(status.get("error") or f"FAL job ended with status {provider_state}")
                    _finish_submission(
                        self.sink,
                        eval_run_id,
                        prompt_id=str(payload["prompt_id"]) if payload.get("prompt_id") is not None else None,
                        grid_cell_id=str(payload["grid_cell_id"]) if payload.get("grid_cell_id") is not None else None,
                        state="failed",
                    )
                    self.sink.mark_failed(eval_run_id, message)
                    raise RuntimeError(message)
                time.sleep(self.poll_seconds)
        except FalProviderError as exc:
            if exc.retryable:
                raise RetryJob(str(exc), delay_seconds=3.0) from exc
            self.sink.mark_failed(eval_run_id, str(exc))
            raise


class FalEvalBatchHandler:
    """Execute every prompt in an EvalRun with durable per-prompt provider state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: FalQueueClient,
        sink: EvalOutputSink,
        request_resolver: RequestResolver,
        *,
        poll_seconds: float = 1.5,
    ):
        self.session_factory = session_factory
        self.client = client
        self.sink = sink
        self.request_resolver = request_resolver
        self.poll_seconds = poll_seconds

    def __call__(self, context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        eval_run_id = str(payload["eval_run_id"])
        definition, checkpoint_id, prompts = self._plan(eval_run_id)
        tasks: dict[str, dict[str, Any]] = dict(payload.get("provider_tasks") or {})
        total = max(len(prompts), 1)
        completed = 0
        for prompt in prompts:
            output_prompt_id = getattr(prompt, "output_prompt_id", prompt.id)
            if self._has_output(eval_run_id, output_prompt_id, inline_prompt_id=prompt.id if output_prompt_id is None else None):
                tasks[prompt.id] = {**tasks.get(prompt.id, {}), "state": "completed"}
                completed += 1
                continue
            task = dict(tasks.get(prompt.id) or {})
            request_payload = {
                "endpoint_id": definition.endpoint,
                "checkpoint_id": checkpoint_id,
                "prompt_id": prompt.id,
                "prompt": prompt.text,
                "parameters": {**definition.parameters, **self._run_parameters(eval_run_id)},
            }
            try:
                if not task.get("request_id"):
                    intent = _submission_intent(self.sink, eval_run_id, prompt_id=prompt.id)
                    request_id = intent.get("provider_request_id") if intent else None
                    if request_id:
                        resume = getattr(self.client, "resume_submission", None)
                        if resume is None:
                            raise RuntimeError("FAL submission requires reconciliation before polling")
                        submission = resume(definition.endpoint, str(request_id))
                    else:
                        request = self.request_resolver(request_payload)
                        submission = self.client.submit(definition.endpoint, request)
                        if intent:
                            record = getattr(self.sink, "record_submission_intent", None)
                            if record is None:
                                raise RuntimeError("FAL submission intent cannot be recorded")
                            record(str(intent["intent_id"]), submission.request_id)
                    task = {
                        "request_id": submission.request_id,
                        "status_url": submission.status_url,
                        "response_url": submission.response_url,
                        "cancel_url": submission.cancel_url,
                        "state": "submitted",
                    }
                    tasks[prompt.id] = task
                    context.checkpoint(provider_tasks=tasks)
                    self.sink.provider_submitted(eval_run_id, submission.request_id)
                while True:
                    if context.cancellation_requested():
                        if task.get("cancel_url"):
                            self.client.cancel(str(task["cancel_url"]))
                        self.sink.mark_canceled(eval_run_id)
                        return {"completed_prompts": completed, "canceled": True}
                    status = self.client.status(str(task["status_url"]))
                    state = str(status.get("status", "")).upper()
                    context.progress((completed + _progress(status)) / total)
                    if state == "COMPLETED":
                        response = self.client.result(str(task["response_url"]))
                        response.setdefault("_request_id", str(task["request_id"]))
                        response.setdefault("prompt", prompt.text)
                        if output_prompt_id is None:
                            response.setdefault("_inline_prompt_id", prompt.id)
                        self.sink.ingest_outputs(eval_run_id, output_prompt_id, response)
                        _finish_submission(self.sink, eval_run_id, prompt_id=prompt.id)
                        task["state"] = "completed"
                        tasks[prompt.id] = task
                        context.checkpoint(provider_tasks=tasks)
                        completed += 1
                        break
                    if state in {"FAILED", "CANCELLED", "CANCELED"}:
                        raise RuntimeError(str(status.get("error") or f"FAL job ended with status {state}"))
                    time.sleep(self.poll_seconds)
            except FalProviderError as exc:
                if exc.retryable:
                    raise RetryJob(str(exc), delay_seconds=3.0) from exc
                self.sink.mark_failed(eval_run_id, str(exc))
                raise
            except Exception as exc:
                self.sink.mark_failed(eval_run_id, str(exc))
                raise
        self.sink.mark_succeeded(eval_run_id)
        return {"completed_prompts": completed, "total_prompts": len(prompts)}

    def _plan(self, eval_run_id: str):
        with self.session_factory() as session:
            run = session.get(models.EvalRun, eval_run_id)
            if not run:
                raise LookupError("eval run not found")
            definition = session.get(models.EvalDefinition, run.definition_id)
            if not definition:
                raise LookupError("eval definition not found")
            checkpoint_id = run.checkpoint_id
            if not checkpoint_id and definition.model_version_id:
                version = session.get(models.ModelVersion, definition.model_version_id)
                checkpoint_id = version.checkpoint_id if version else None
            if not checkpoint_id:
                raise ValueError("eval run requires a checkpoint or model version")
            inline = sorted(list(definition.inline_prompts or []), key=lambda item: (int(item.get("position", 0)), str(item.get("id", ""))))
            prompts = [SimpleNamespace(id=str(item["id"]), text=str(item["text"]), output_prompt_id=None, metadata_=dict(item.get("metadata") or {})) for item in inline]
            if not prompts:
                raise ValueError("eval run requires immutable inline case snapshot; migration required")
            session.expunge(definition)
            return definition, checkpoint_id, prompts

    def _run_parameters(self, eval_run_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            run = session.get(models.EvalRun, eval_run_id)
            return dict(run.parameters_snapshot) if run else {}

    def _has_output(self, eval_run_id: str, prompt_id: str | None, *, inline_prompt_id: str | None = None) -> bool:
        with self.session_factory() as session:
            query = select(models.EvalOutput.id).where(models.EvalOutput.eval_run_id == eval_run_id)
            if prompt_id is not None:
                query = query.where(models.EvalOutput.prompt_id == prompt_id)
            elif inline_prompt_id is not None:
                query = query.where(models.EvalOutput.prompt_id.is_(None), models.EvalOutput.provider_metadata["inline_prompt_id"].as_string() == inline_prompt_id)
            else:
                return False
            return session.scalar(query.limit(1)) is not None


class FalGridBatchHandler:
    """Execute a saved grid with restart-safe state for every provider cell."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: FalQueueClient,
        sink: EvalOutputSink,
        request_resolver: RequestResolver,
        *,
        poll_seconds: float = 1.5,
    ):
        self.session_factory = session_factory
        self.client = client
        self.sink = sink
        self.request_resolver = request_resolver
        self.poll_seconds = poll_seconds

    def __call__(self, context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        grid_id = str(payload["grid_definition_id"])
        eval_run_id = str(payload.get("eval_run_id") or self._create_eval_run(grid_id))
        if not payload.get("eval_run_id"):
            payload["eval_run_id"] = eval_run_id
            context.checkpoint(eval_run_id=eval_run_id)
        plans = self._plan(grid_id, payload.get("cell_ordinals"))
        tasks: dict[str, dict[str, Any]] = dict(payload.get("grid_tasks") or {})
        completed = 0
        total = max(len(plans), 1)
        try:
            for plan in plans:
                x_index, y_index = int(plan["x_index"]), int(plan["y_index"])
                key = str(plan.get("grid_cell_id") or f"{plan.get('z_index', -1)}:{y_index}:{x_index}")
                if plan.get("status") == "succeeded" or self.sink.grid_cell_exists(
                    grid_id,
                    x_index,
                    y_index,
                    **({"grid_cell_id": plan["grid_cell_id"]} if plan.get("grid_cell_id") is not None else {}),
                ):
                    tasks[key] = {**tasks.get(key, {}), "state": "completed"}
                    completed += 1
                    continue
                task = dict(tasks.get(key) or {})
                if not task.get("request_id"):
                    request = dict(plan.get("effective_params") or {})
                    if not request:
                        request = self.request_resolver(plan)
                    submission = self.client.submit(str(plan["endpoint_id"]), request)
                    task = {
                        "request_id": submission.request_id,
                        "status_url": submission.status_url,
                        "response_url": submission.response_url,
                        "cancel_url": submission.cancel_url,
                        "state": "submitted",
                    }
                    tasks[key] = task
                    context.checkpoint(grid_tasks=tasks)
                    if plan.get("grid_cell_id") is None:
                        self.sink.provider_submitted(eval_run_id, submission.request_id)
                    else:
                        self.sink.provider_submitted(eval_run_id, submission.request_id, grid_cell_id=plan["grid_cell_id"])
                while True:
                    if context.cancellation_requested():
                        if task.get("cancel_url"):
                            self.client.cancel(str(task["cancel_url"]))
                        self.sink.mark_canceled(eval_run_id)
                        return {"grid_definition_id": grid_id, "eval_run_id": eval_run_id, "completed_cells": completed, "total_cells": len(plans), "canceled": True}
                    provider = self.client.status(str(task["status_url"]))
                    state = str(provider.get("status", "")).upper()
                    context.progress((completed + _progress(provider)) / total)
                    if state == "COMPLETED":
                        response = self.client.result(str(task["response_url"]))
                        response["_grid_metadata"] = {
                            "parameters": {
                                **dict(plan.get("effective_params") or plan.get("parameters") or {}),
                                **({"lora_scale": plan["lora_scale"]} if plan.get("lora_scale") is not None else {}),
                            },
                            "checkpoint_id": plan.get("checkpoint_id"),
                            "model_version_id": plan.get("model_version_id"),
                            "checkpoint_revision_id": plan.get("checkpoint_revision_id"),
                            "grid_cell_id": plan.get("grid_cell_id"),
                            "grid_definition_id": grid_id,
                            "x_index": x_index,
                            "y_index": y_index,
                            "z_index": plan.get("z_index"),
                            "coordinates": dict(plan.get("coordinates") or {}),
                        }
                        response.setdefault("_request_id", str(task["request_id"]))
                        response.setdefault("_endpoint_id", str(plan["endpoint_id"]))
                        if plan.get("prompt_id") is None:
                            response.setdefault("_inline_prompt_id", str(plan.get("case_id") or ""))
                            response.setdefault("prompt", str(plan.get("prompt") or ""))
                        ingested = self.sink.ingest_outputs(eval_run_id, plan.get("prompt_id"), response)
                        for output_id in ingested.get("output_ids", []):
                            if plan.get("grid_cell_id") is None:
                                self.sink.link_grid_cell(grid_id, str(output_id), x_index, y_index)
                            else:
                                self.sink.link_grid_cell(grid_id, str(output_id), x_index, y_index, grid_cell_id=plan["grid_cell_id"])
                        task.update(state="completed", output_ids=ingested.get("output_ids", []))
                        tasks[key] = task
                        context.checkpoint(grid_tasks=tasks)
                        completed += 1
                        break
                    if state in {"FAILED", "CANCELLED", "CANCELED"}:
                        raise RuntimeError(str(provider.get("error") or f"FAL grid cell ended with status {state}"))
                    time.sleep(self.poll_seconds)
        except FalProviderError as exc:
            if exc.retryable:
                raise RetryJob(str(exc), delay_seconds=3.0) from exc
            self.sink.mark_failed(eval_run_id, str(exc))
            raise
        except Exception as exc:
            self.sink.mark_failed(eval_run_id, str(exc))
            raise
        self.sink.mark_succeeded(eval_run_id)
        return {"grid_definition_id": grid_id, "eval_run_id": eval_run_id, "completed_cells": completed, "total_cells": len(plans)}

    def _create_eval_run(self, grid_id: str) -> str:
        with self.session_factory.begin() as session:
            grid = _required(session, models.GridDefinition, grid_id)
            definition = _required(session, models.EvalDefinition, grid.eval_definition_id)
            checkpoint_id = None
            if definition.model_version_id:
                version = _required(session, models.ModelVersion, definition.model_version_id)
                checkpoint_id = version.checkpoint_id
            run = models.EvalRun(definition_id=definition.id, checkpoint_id=checkpoint_id, status="queued", parameters_snapshot=dict(definition.parameters))
            session.add(run)
            session.flush()
            return run.id

    def _plan(self, grid_id: str, cell_ordinals: list[int] | None = None) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            grid = _required(session, models.GridDefinition, grid_id)
            if not grid.plan_snapshot or not grid.plan_id:
                raise ValueError("grid lacks immutable experiment plan; migration required before worker execution")
            cells = list(session.scalars(select(models.GridCell).where(models.GridCell.grid_definition_id == grid.id).order_by(models.GridCell.ordinal)))
            if cell_ordinals is not None:
                requested = set(int(item) for item in cell_ordinals)
                cells = [cell for cell in cells if cell.ordinal in requested]
            plans: list[dict[str, Any]] = []
            for cell in cells:
                target = dict(cell.target_snapshot or {})
                case = dict(cell.case_snapshot or {})
                input_value = dict(case.get("input") or {})
                checkpoint_id = None
                version_id = target.get("model_version_id")
                if version_id:
                    version = session.get(models.ModelVersion, str(version_id))
                    checkpoint_id = version.checkpoint_id if version else None
                if target.get("checkpoint_revision_id") and not checkpoint_id:
                    revision = session.get(models.CheckpointRevision, str(target["checkpoint_revision_id"]))
                    checkpoint_id = revision.checkpoint_id if revision else None
                plans.append({
                    "endpoint_id": cell.endpoint_id,
                    "checkpoint_id": checkpoint_id,
                    "prompt_id": None,
                    "case_id": case.get("case_id"),
                    "prompt": str(input_value.get("prompt") or input_value.get("text") or ""),
                    "effective_params": dict(cell.effective_params or {}),
                    "model_version_id": version_id,
                    "checkpoint_revision_id": target.get("checkpoint_revision_id"),
                    "grid_cell": True,
                    "grid_cell_id": cell.id,
                    "grid_definition_id": grid.id,
                    "x_index": int(cell.x_index),
                    "y_index": int(cell.y_index),
                    "z_index": cell.z_index,
                    "coordinates": dict(cell.coordinate or {}),
                    "status": cell.status,
                })
            return plans






def _finish_submission(
    sink: EvalOutputSink,
    eval_run_id: str,
    *,
    prompt_id: str | None = None,
    grid_cell_id: str | None = None,
    state: str = "completed",
) -> None:
    finish = getattr(sink, "finish_submission_intent", None)
    if finish is not None:
        finish(eval_run_id, prompt_id=prompt_id, grid_cell_id=grid_cell_id, state=state)
def _required(session: Session, model, object_id: str):
    value = session.get(model, object_id)
    if value is None:
        raise LookupError(f"{model.__name__} not found: {object_id}")
    return value


def _progress(status: dict[str, Any]) -> float:
    if str(status.get("status", "")).upper() == "COMPLETED":
        return 0.95
    position = status.get("queue_position")
    if isinstance(position, int) and position >= 0:
        return max(0.05, min(0.5, 0.5 / (position + 1)))
    return 0.55 if str(status.get("status", "")).upper() == "IN_PROGRESS" else 0.1


def _submission_intent(
    sink: EvalOutputSink,
    eval_run_id: str,
    *,
    prompt_id: str | None = None,
    grid_cell_id: str | None = None,
) -> dict[str, Any] | None:
    lookup = getattr(sink, "submission_intent", None)
    if lookup is None:
        return None
    return lookup(eval_run_id, prompt_id=prompt_id, grid_cell_id=grid_cell_id)
