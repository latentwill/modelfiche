from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .training_metrics import (
    MetricBatchConflict,
    MetricInput,
    MetricValidationError,
    append_run_event,
    ingest_metric_batch,
)
from .wandb_auth import WandbPrincipal
from .wandb_uploads import UploadError, capture_media_metadata, upload_instruction


GRAPHQL_DOCUMENTS = {
    "4ebf7664b24d74a90fa96ce71f8616d9838f25bc5f8b0c41e4223bc0144a2991": "ServerFeaturesQuery",
    "a5605080f3769fa833cde5195012079fce1f2138f52377db5861d4c1b86a6082": "RunResumeStatus",
    "a30f4b9373ef4d200d9e927fe21315320dc83fc409c70a7ed64201a2b107bf6d": "UpsertBucket",
    "ded42700181e7cc22cee9fd2fa9b41c440b7a329674603cb5da6390ef0305a60": "OrganizationCoreWeaveOrganizationID",
    "ba754af80fea63fe2deab1ade3a1429d0a018856f5af4996ab86a55936ff07a7": "Viewer",
    "66f378732e60ffebf606025796ebd6515971be93fae2716730ef9a0fe6532997": "CreateRunFiles",
}
SUPPORTED_FILESTREAM_FILES = frozenset({"wandb-history.jsonl", "wandb-summary.json", "output.log", "wandb-events.jsonl"})


class ProtocolError(ValueError):
    pass


def graphql_operation(body: object) -> tuple[str, dict[str, Any]]:
    if not isinstance(body, dict):
        raise ProtocolError("GraphQL request body must be an object")
    query = body.get("query")
    variables = body.get("variables", {})
    if not isinstance(query, str) or not isinstance(variables, dict):
        raise ProtocolError("GraphQL query and variables are required")
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    operation = GRAPHQL_DOCUMENTS.get(digest)
    if operation is None:
        raise ProtocolError("unsupported W&B GraphQL document")
    if body.get("operationName") not in (None, "", operation):
        raise ProtocolError("GraphQL operationName does not match the approved document")
    return operation, variables


def _validate_identity(principal: WandbPrincipal, *, entity: object, project: object, run: object | None = None) -> None:
    expected = principal.run
    if entity != "dam" or project != expected.wandb_project:
        raise ProtocolError("W&B entity or project does not match the training launch")
    if run is not None and run != expected.wandb_run_id:
        raise ProtocolError("W&B run ID does not match the training launch")


def _decoded_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


_SENSITIVE_CONFIG_KEY = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|secret(?:_?key)?|private_?key|token|password|passwd|authorization|credentials?|cookie)(?:$|_)",
    re.IGNORECASE,
)


def _normalized_config_key(key: str) -> str:
    snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return re.sub(r"[^a-zA-Z0-9]+", "_", snake_case)


def _redact_config(value: object, *, key: str | None = None) -> object:
    if key is not None and _SENSITIVE_CONFIG_KEY.search(_normalized_config_key(key)):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_config(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def _config_values(config: object) -> dict[str, object]:
    decoded = _decoded_object(config)
    if decoded is None:
        return {}
    redacted = _redact_config(decoded)
    assert isinstance(redacted, dict)
    output: dict[str, object] = {}
    for key, raw in redacted.items():
        output[key] = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
    return output


def _run_bucket(principal: WandbPrincipal) -> dict[str, object]:
    run = principal.run
    return {
        "id": run.id,
        "name": run.wandb_run_id,
        "displayName": run.wandb_display_name,
        "description": None,
        "config": json.dumps(run.normalized_config, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        "project": {
            "id": run.project_id,
            "name": run.wandb_project,
            "entity": {"id": principal.launch.workspace_id, "name": "dam"},
        },
        "historyLineCount": 0,
    }


def handle_graphql(
    session: Session,
    principal: WandbPrincipal,
    body: object,
    *,
    base_url: str,
) -> dict[str, object]:
    operation, variables = graphql_operation(body)
    run = principal.run
    launch = principal.launch

    if operation == "ServerFeaturesQuery":
        if variables:
            raise ProtocolError("ServerFeaturesQuery does not accept variables")
        return {"serverInfo": {"features": []}}

    if operation == "Viewer":
        return {
            "viewer": {
                "id": principal.credential.id,
                "entity": "dam",
                "username": principal.credential.alias,
                "email": "wandb-ingress@invalid",
                "flags": 0,
                "teams": {"edges": []},
            },
            "serverInfo": {"cliVersionInfo": {"max_cli_version": "0.28.0"}},
        }

    if operation == "OrganizationCoreWeaveOrganizationID":
        if variables.get("entityName") != "dam":
            raise ProtocolError("organization entity does not match the training launch")
        return {"entity": {"organization": {"coreWeaveOrganizationId": None}}}

    if operation == "RunResumeStatus":
        _validate_identity(
            principal,
            entity=variables.get("entity"),
            project=variables.get("project"),
            run=variables.get("name"),
        )
        bucket = _run_bucket(principal) if run.status != "pending" else None
        return {"model": {"bucket": bucket}}

    if operation == "UpsertBucket":
        _validate_identity(
            principal,
            entity=variables.get("entity"),
            project=variables.get("project"),
            run=variables.get("name"),
        )
        display_name = variables.get("displayName")
        if display_name not in (None, run.wandb_display_name):
            raise ProtocolError("W&B display name does not match the training launch")
        inserted = run.status == "pending"
        raw_config = variables.get("config")
        captured_config = _config_values(raw_config)
        if captured_config:
            run.normalized_config = {**dict(run.normalized_config or {}), "wandb": captured_config}
            run.raw_manifest = {
                **dict(run.raw_manifest or {}),
                "wandb_config": _redact_config(_decoded_object(raw_config) or {}),
            }
        if inserted:
            now = models.utcnow()
            run.status = "running"
            run.started_at = now
            run.last_event_at = now
            launch.state = "running"
            append_run_event(
                session,
                run,
                type="run.started",
                idempotency_key=f"wandb:init:{launch.id}",
                payload={"wandb_run_id": run.wandb_run_id, "sdk_version": run.wandb_sdk_version},
                occurred_at=now,
            )
        return {"upsertBucket": {"bucket": _run_bucket(principal), "inserted": inserted}}

    if operation == "CreateRunFiles":
        _validate_identity(
            principal,
            entity=variables.get("entity"),
            project=variables.get("project"),
            run=variables.get("run"),
        )
        files = variables.get("files")
        if not isinstance(files, list) or any(not isinstance(name, str) for name in files):
            raise ProtocolError("CreateRunFiles requires a list of file names")
        if len(files) > 32:
            raise ProtocolError("CreateRunFiles batch exceeds the supported file count")
        instructions = []
        for name in files:
            try:
                upload, upload_url = upload_instruction(
                    session,
                    run,
                    launch,
                    name,
                    fallback_base_url=base_url,
                )
            except UploadError as exc:
                raise ProtocolError(str(exc)) from exc
            instructions.append({"name": upload.relative_path, "uploadUrl": upload_url})
        return {
            "createRunFiles": {
                "runID": run.wandb_run_id,
                "uploadHeaders": [],
                "files": instructions,
            }
        }

    raise ProtocolError("unsupported W&B GraphQL operation")




def validate_path_identity(principal: WandbPrincipal, entity: str, project: str, run_id: str) -> None:
    _validate_identity(principal, entity=entity, project=project, run=run_id)


def _summary_value(value: object) -> tuple[str, float | None, str | None] | None:
    if value is None:
        return "null", None, None
    if isinstance(value, bool):
        return "text", None, "true" if value else "false"
    if isinstance(value, (int, float)):
        number = float(value)
        return ("number", number, None) if math.isfinite(number) else None
    if isinstance(value, str) and len(value) <= 16_384:
        return "text", None, value
    return None


def apply_summary(session: Session, run: models.TrainingRun, summary_payload: dict[str, object]) -> None:
    step_value = summary_payload.get("_step", run.current_step if run.current_step is not None else 0)
    step = step_value if isinstance(step_value, int) and not isinstance(step_value, bool) and step_value >= 0 else 0
    for name, value in summary_payload.items():
        if name.startswith("_") or not isinstance(name, str) or len(name) > 120:
            continue
        typed = _summary_value(value)
        if typed is None:
            continue
        value_type, numeric, text = typed
        row = session.scalar(
            select(models.RunMetricSummary).where(
                models.RunMetricSummary.run_id == run.id,
                models.RunMetricSummary.name == name,
            )
        )
        if row is None:
            row = models.RunMetricSummary(run_id=run.id, name=name, last_step=step, value_type=value_type)
            session.add(row)
        row.last_step = step
        row.last_value = numeric
        row.last_value_text = text
        row.value_type = value_type
        if numeric is not None:
            row.minimum = numeric if row.minimum is None else min(row.minimum, numeric)
            row.maximum = numeric if row.maximum is None else max(row.maximum, numeric)
        row.updated_at = models.utcnow()


def handle_filestream(session: Session, principal: WandbPrincipal, body: object) -> dict[str, object]:
    if not isinstance(body, dict):
        raise ProtocolError("filestream body must be an object")
    files = body.get("files", {})
    if not isinstance(files, dict):
        raise ProtocolError("filestream files must be an object")
    unsupported = sorted(set(files) - SUPPORTED_FILESTREAM_FILES)
    if unsupported:
        raise ProtocolError(f"unsupported W&B filestream files: {', '.join(unsupported)}")

    run = principal.run
    launch = principal.launch
    now = models.utcnow()
    if launch.state == "running" and run.started_at is not None and run.finished_at is None:
        # A live filestream is authoritative evidence that the trainer is
        # active; recover inconsistent non-terminal projections.
        run.status = "running"
    run.last_event_at = now
    for filename, raw_segment in files.items():
        if not isinstance(raw_segment, dict):
            raise ProtocolError(f"{filename} filestream segment must be an object")
        content = raw_segment.get("content", [])
        offset = raw_segment.get("offset", 0)
        if not isinstance(content, list) or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ProtocolError(f"{filename} filestream segment has invalid content or offset")
        if len(content) > 10_000:
            raise ProtocolError(f"{filename} filestream segment exceeds the supported row count")
        batch_payload = {"filename": filename, "offset": offset, "content": content}
        batch_key = "wandb:" + hashlib.sha256(
            json.dumps(batch_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        if filename == "wandb-history.jsonl":
            points: list[MetricInput] = []
            committed_step: int | None = None
            for raw_row in content:
                row = _decoded_object(raw_row)
                if row is None:
                    raise ProtocolError("wandb-history.jsonl content must contain JSON objects")
                step = row.get("_step")
                if not isinstance(step, int) or isinstance(step, bool) or step < 0:
                    raise ProtocolError("W&B history row is missing a valid _step")
                try:
                    capture_media_metadata(session, run, launch, row)
                except UploadError as exc:
                    raise ProtocolError(str(exc)) from exc
                committed_step = max(committed_step or step, step)
                timestamp = row.get("_timestamp")
                wall_time = float(timestamp) if isinstance(timestamp, (int, float)) and math.isfinite(float(timestamp)) else None
                for name, value in row.items():
                    if name.startswith("_") or isinstance(value, (dict, list)):
                        continue
                    points.append(MetricInput(step=step, name=name, value=value, wall_time=wall_time, source_key=filename))
            try:
                ingest_metric_batch(
                    session,
                    run.id,
                    points,
                    batch_key=batch_key,
                    committed_step=committed_step,
                    occurred_at=now,
                )
            except (MetricValidationError, MetricBatchConflict) as exc:
                raise ProtocolError(str(exc)) from exc
        elif filename == "wandb-summary.json" and content:
            summary = _decoded_object(content[-1])
            if summary is None:
                raise ProtocolError("wandb-summary.json content must contain JSON objects")
            try:
                capture_media_metadata(session, run, launch, summary)
            except UploadError as exc:
                raise ProtocolError(str(exc)) from exc
            apply_summary(session, run, summary)

    if body.get("complete") is True:
        exit_code = body.get("exitcode", 0)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ProtocolError("filestream exitcode must be an integer")
        finish_run(session, principal, exit_code=exit_code, signal="filestream.complete")
    return {}


def finish_run(session: Session, principal: WandbPrincipal, *, exit_code: int, signal: str) -> None:
    run = principal.run
    if run.status in {"completed", "failed"}:
        return
    now = models.utcnow()
    run.status = "completed" if exit_code == 0 else "failed"
    run.exit_code = exit_code
    run.finished_at = now
    run.last_event_at = now
    principal.launch.state = run.status
    append_run_event(
        session,
        run,
        type=f"run.{run.status}",
        idempotency_key=f"wandb:finish:{run.id}:{exit_code}",
        payload={"exit_code": exit_code, "signal": signal},
        step=run.current_step,
        occurred_at=now,
    )
