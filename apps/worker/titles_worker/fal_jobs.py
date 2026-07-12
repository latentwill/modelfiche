from __future__ import annotations

import time
from itertools import product
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from titles_api import models
from titles_api.integrations.fal.client import FalProviderError, FalQueueClient

from .runner import JobContext, RetryJob


class EvalOutputSink(Protocol):
    def provider_submitted(self, eval_run_id: str, request_id: str) -> None: ...

    def ingest_outputs(self, eval_run_id: str, prompt_id: str | None, response: dict[str, Any]) -> dict[str, Any]: ...

    def mark_failed(self, eval_run_id: str, message: str) -> None: ...

    def mark_succeeded(self, eval_run_id: str) -> None: ...

    def mark_canceled(self, eval_run_id: str) -> None: ...

    def link_grid_cell(self, grid_definition_id: str, output_id: str, x_index: int, y_index: int) -> None: ...

    def grid_cell_exists(self, grid_definition_id: str, x_index: int, y_index: int) -> bool: ...


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
            if self._has_output(eval_run_id, prompt.id):
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
                        self.sink.ingest_outputs(eval_run_id, prompt.id, response)
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
            prompts = list(
                session.scalars(
                    select(models.Prompt)
                    .where(models.Prompt.prompt_set_id == definition.prompt_set_id)
                    .order_by(models.Prompt.position, models.Prompt.id)
                )
            )
            if not prompts:
                raise ValueError("eval prompt set is empty")
            session.expunge(definition)
            for prompt in prompts:
                session.expunge(prompt)
            return definition, checkpoint_id, prompts

    def _run_parameters(self, eval_run_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            run = session.get(models.EvalRun, eval_run_id)
            return dict(run.parameters_snapshot) if run else {}

    def _has_output(self, eval_run_id: str, prompt_id: str) -> bool:
        with self.session_factory() as session:
            return session.scalar(
                select(models.EvalOutput.id).where(
                    models.EvalOutput.eval_run_id == eval_run_id,
                    models.EvalOutput.prompt_id == prompt_id,
                ).limit(1)
            ) is not None


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
        plans = self._plan(grid_id)
        tasks: dict[str, dict[str, Any]] = dict(payload.get("grid_tasks") or {})
        completed = 0
        total = max(len(plans), 1)
        try:
            for plan in plans:
                x_index, y_index = int(plan["x_index"]), int(plan["y_index"])
                key = f"{y_index}:{x_index}"
                if self.sink.grid_cell_exists(grid_id, x_index, y_index):
                    tasks[key] = {**tasks.get(key, {}), "state": "completed"}
                    completed += 1
                    continue
                task = dict(tasks.get(key) or {})
                if not task.get("request_id"):
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
                    self.sink.provider_submitted(eval_run_id, submission.request_id)
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
                        response.setdefault("_request_id", str(task["request_id"]))
                        ingested = self.sink.ingest_outputs(eval_run_id, plan.get("prompt_id"), response)
                        for output_id in ingested.get("output_ids", []):
                            self.sink.link_grid_cell(grid_id, str(output_id), x_index, y_index)
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
            if not checkpoint_id:
                raise ValueError("grid requires a model version with a checkpoint")
            run = models.EvalRun(definition_id=definition.id, checkpoint_id=checkpoint_id, status="queued", parameters_snapshot=dict(definition.parameters))
            session.add(run)
            session.flush()
            return run.id

    def _plan(self, grid_id: str) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            grid = _required(session, models.GridDefinition, grid_id)
            definition = _required(session, models.EvalDefinition, grid.eval_definition_id)
            if not definition.model_version_id:
                raise ValueError("grid eval definition requires a model version")
            version = _required(session, models.ModelVersion, definition.model_version_id)
            base_checkpoint = _required(session, models.Checkpoint, version.checkpoint_id)
            prompts = list(session.scalars(select(models.Prompt).where(models.Prompt.prompt_set_id == definition.prompt_set_id).order_by(models.Prompt.position, models.Prompt.id)))
            if not prompts:
                raise ValueError("grid prompt set is empty")
            x_field = str(grid.x_axis.get("parameter") or grid.x_axis.get("field") or "")
            y_field = str(grid.y_axis.get("parameter") or grid.y_axis.get("field") or "")
            x_values, y_values = list(grid.x_axis.get("values") or []), list(grid.y_axis.get("values") or [])
            if not x_field or not y_field or x_field == y_field or not x_values or not y_values:
                raise ValueError("grid requires two different, non-empty axes")
            plans: list[dict[str, Any]] = []
            for (x_index, x_value), (y_index, y_value) in product(enumerate(x_values), enumerate(y_values)):
                prompt_id, prompt = prompts[0].id, prompts[0].text
                checkpoint = base_checkpoint
                parameters = dict(definition.parameters)
                lora_scale = float(parameters.pop("lora_scale", 1.0))
                for field, value in ((x_field, x_value), (y_field, y_value)):
                    if field == "prompt":
                        prompt_id, prompt = self._prompt(prompts, value)
                    elif field == "checkpoint_step":
                        checkpoint = session.scalar(select(models.Checkpoint).where(models.Checkpoint.run_id == base_checkpoint.run_id, models.Checkpoint.step == int(value)))
                        if checkpoint is None:
                            raise ValueError(f"checkpoint step is unavailable: {value}")
                    elif field == "model":
                        selected = session.get(models.ModelVersion, str(value))
                        if selected is None:
                            raise ValueError(f"model version is unavailable: {value}")
                        checkpoint = _required(session, models.Checkpoint, selected.checkpoint_id)
                    elif field == "lora_scale":
                        lora_scale = float(value)
                    else:
                        parameters[field] = _axis_value(field, value)
                plans.append({
                    "endpoint_id": definition.endpoint,
                    "checkpoint_id": checkpoint.id,
                    "prompt_id": prompt_id,
                    "prompt": prompt,
                    "parameters": parameters,
                    "lora_scale": lora_scale,
                    "grid_cell": True,
                    "grid_definition_id": grid.id,
                    "x_index": x_index,
                    "y_index": y_index,
                    "coordinates": {x_field: x_value, y_field: y_value},
                })
            return plans

    @staticmethod
    def _prompt(prompts: list[models.Prompt], value: Any) -> tuple[str | None, str]:
        if isinstance(value, dict):
            prompt_id = value.get("id")
            text = value.get("text") or value.get("prompt")
            if text:
                return str(prompt_id) if prompt_id else None, str(text)
        for prompt in prompts:
            if str(value) in {prompt.id, prompt.text}:
                return prompt.id, prompt.text
        return None, str(value)


def _axis_value(field: str, value: Any) -> Any:
    if field == "seed":
        return int(value)
    if field == "enable_prompt_expansion":
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}
    return value



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
