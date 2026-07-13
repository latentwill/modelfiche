from __future__ import annotations

import json
import hashlib
import os
import signal
import subprocess
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import typer

from . import __version__
from .client import ApiError, LocalContext, TitlesClient
from .render import emit, fail


app = typer.Typer(name="titles", no_args_is_help=True, help="Titles DAM operator and agent CLI")


@dataclass(slots=True)
class Runtime:
    context: LocalContext
    profile: str | None
    workspace: str | None
    request_id: str | None
    timeout: float
    json_output: bool
    jsonl: bool
    quiet: bool

    def client(self) -> TitlesClient:
        return TitlesClient(
            base_url=self.context.api_url,
            profile=self.profile or self.context.profile,
            workspace=self.workspace or self.context.workspace,
            request_id=self.request_id,
            timeout=self.timeout,
        )


@app.callback()
def root(
    ctx: typer.Context,
    api_url: str | None = typer.Option(None, envvar="TITLES_API_URL"),
    profile: str | None = typer.Option(None, "--profile"),
    workspace: str | None = typer.Option(None, "--workspace"),
    request_id: str | None = typer.Option(None, "--request-id"),
    timeout: float = typer.Option(30.0, min=0.1),
    json_output: bool = typer.Option(False, "--json"),
    jsonl: bool = typer.Option(False, "--jsonl"),
    quiet: bool = typer.Option(False, "--quiet"),
) -> None:
    local = LocalContext.load()
    if api_url:
        local.api_url = api_url
    ctx.obj = Runtime(local, profile, workspace, request_id, timeout, json_output, jsonl, quiet)


def rt(ctx: typer.Context) -> Runtime:
    return ctx.find_root().obj


def call(ctx: typer.Context, operation: Callable[[TitlesClient], Any]) -> Any:
    runtime = rt(ctx)
    client = runtime.client()
    try:
        result = operation(client)
        emit(result, json_output=runtime.json_output, jsonl=runtime.jsonl)
        return result
    except ApiError as exc:
        fail(exc, json_output=runtime.json_output)
    finally:
        client.close()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"Cannot read JSON from {path}: {exc}") from exc


def parse_params(values: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        if "=" not in value:
            raise typer.BadParameter(f"Expected key=value, got {value!r}")
        key, raw = value.split("=", 1)
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def split_subject(subject: str) -> tuple[str, str]:
    if ":" not in subject:
        raise typer.BadParameter("Subject must use typed form, for example asset:<uuid>")
    subject_type, subject_id = subject.split(":", 1)
    if not subject_type or not subject_id:
        raise typer.BadParameter("Subject type and ID are required")
    return subject_type, subject_id


def unsupported(ctx: typer.Context, command: str, reason: str, *, alternative: str | None = None) -> None:
    details = {"command": command}
    if alternative:
        details["alternative"] = alternative
    fail(
        ApiError(
            reason,
            status_code=5,
            code="UNSUPPORTED_OPERATION",
            details=details,
        ),
        json_output=rt(ctx).json_output,
    )


def s3_object(client: TitlesClient, source: str, key: str) -> dict[str, Any]:
    normalized = key.strip().lstrip("/")
    if not normalized or normalized.endswith("/"):
        raise ApiError(
            "S3 stat requires an object key, not a prefix",
            status_code=5,
            code="INVALID_OBJECT_KEY",
        )
    parent, separator, _ = normalized.rpartition("/")
    prefix = f"{parent}/" if separator else normalized
    cursor: str | None = None
    while True:
        page = client.get(
            f"/api/import-sources/{source}/browse",
            prefix=prefix,
            cursor=cursor,
            page_size=1000,
        )
        for item in page.get("objects", []):
            if item.get("key") == normalized:
                return item
        cursor = page.get("next_cursor")
        if not cursor:
            raise ApiError(
                f"S3 object was not found: {normalized}",
                status_code=4,
                code="S3_OBJECT_NOT_FOUND",
            )


def s3_prefix_tree(client: TitlesClient, source: str, prefix: str, depth: int, limit: int) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    pending = [(prefix, 0)]
    visited: set[str] = set()
    truncated = False

    while pending and len(nodes) < limit:
        current, level = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        cursor: str | None = None
        while len(nodes) < limit:
            page = client.get(
                f"/api/import-sources/{source}/browse",
                prefix=current,
                cursor=cursor,
                page_size=min(1000, max(1, limit - len(nodes))),
            )
            child_prefixes = page.get("prefixes", [])
            for child in child_prefixes:
                if len(nodes) >= limit:
                    truncated = True
                    break
                nodes.append({"type": "prefix", "key": child, "depth": level + 1})
                if level + 1 < depth:
                    pending.append((child, level + 1))
            for item in page.get("objects", []):
                if len(nodes) >= limit:
                    truncated = True
                    break
                nodes.append({"type": "object", "depth": level, **item})
            cursor = page.get("next_cursor")
            if not cursor or len(nodes) >= limit:
                truncated = truncated or bool(cursor)
                break
    truncated = truncated or bool(pending)
    return {"prefix": prefix, "depth": depth, "nodes": nodes, "truncated": truncated}


@app.command("version")
def version(ctx: typer.Context) -> None:
    runtime = rt(ctx)
    payload = {"cli_version": __version__}
    try:
        client = runtime.client()
        payload["server"] = client.get("/api/system/version")
        client.close()
    except ApiError:
        payload["server"] = None
    emit(payload, json_output=runtime.json_output)


@app.command("capabilities")
def capabilities(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/system/capabilities"))


@app.command("enums")
def enums(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/system/enums"))


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/system/doctor"))


context_app = typer.Typer(no_args_is_help=True)
app.add_typer(context_app, name="context")


@context_app.command("show")
def context_show(ctx: typer.Context) -> None:
    runtime = rt(ctx)
    emit(
        {
            "api_url": runtime.context.api_url,
            "profile": runtime.profile or runtime.context.profile,
            "workspace": runtime.workspace or runtime.context.workspace,
        },
        json_output=runtime.json_output,
    )


@context_app.command("set")
def context_set(
    ctx: typer.Context,
    api_url: str | None = typer.Option(None),
    profile: str | None = typer.Option(None),
    workspace: str | None = typer.Option(None),
) -> None:
    runtime = rt(ctx)
    if api_url is not None:
        runtime.context.api_url = api_url
    if profile is not None:
        runtime.context.profile = profile
    if workspace is not None:
        runtime.context.workspace = workspace
    runtime.context.save()
    context_show(ctx)


profile_app = typer.Typer(no_args_is_help=True)
app.add_typer(profile_app, name="profile")


@profile_app.command("list")
def profile_list(ctx: typer.Context, include_archived: bool = False) -> None:
    call(ctx, lambda client: client.get("/api/profiles", include_archived=include_archived))


@profile_app.command("create")
def profile_create(
    ctx: typer.Context,
    name: str = typer.Option(...),
    email: str | None = typer.Option(None),
    color: str | None = typer.Option(None),
) -> None:
    call(ctx, lambda client: client.post("/api/profiles", {"display_name": name, "email": email, "avatar_color": color}))


@profile_app.command("show")
def profile_show(ctx: typer.Context, profile: str) -> None:
    call(ctx, lambda client: client.get(f"/api/profiles/{profile}"))


@profile_app.command("use")
def profile_use(ctx: typer.Context, profile: str) -> None:
    runtime = rt(ctx)
    client = runtime.client()
    try:
        value = client.request("PUT", f"/api/profiles/{profile}/active")
        runtime.context.profile = profile
        runtime.context.save()
        emit(value, json_output=runtime.json_output, jsonl=runtime.jsonl)
    except ApiError as exc:
        fail(exc, json_output=runtime.json_output)
    finally:
        client.close()


@profile_app.command("update")
def profile_update(
    ctx: typer.Context,
    profile: str,
    name: str | None = typer.Option(None),
    email: str | None = typer.Option(None),
    color: str | None = typer.Option(None),
) -> None:
    call(ctx, lambda client: client.patch(f"/api/profiles/{profile}", {"display_name": name, "email": email, "avatar_color": color}))


@profile_app.command("archive")
def profile_archive(ctx: typer.Context, profile: str) -> None:
    call(ctx, lambda client: client.post(f"/api/profiles/{profile}/archive"))


project_app = typer.Typer(no_args_is_help=True)
app.add_typer(project_app, name="project")


@project_app.command("list")
def project_list(ctx: typer.Context, state: str | None = None) -> None:
    call(ctx, lambda client: client.get("/api/projects", state=state))


@project_app.command("create")
def project_create(
    ctx: typer.Context,
    name: str = typer.Option(...),
    description: str | None = typer.Option(None),
    trigger: list[str] = typer.Option([], "--trigger"),
) -> None:
    call(ctx, lambda client: client.post("/api/projects", {"title": name, "description": description, "trigger_words": trigger}))


@project_app.command("show")
def project_show(ctx: typer.Context, project: str) -> None:
    call(ctx, lambda client: client.get(f"/api/projects/{project}"))


@project_app.command("update")
def project_update(
    ctx: typer.Context,
    project: str,
    name: str | None = typer.Option(None),
    description: str | None = typer.Option(None),
    state: str | None = typer.Option(None),
) -> None:
    call(ctx, lambda client: client.patch(f"/api/projects/{project}", {"title": name, "description": description, "state": state}))


@project_app.command("archive")
def project_archive(ctx: typer.Context, project: str) -> None:
    call(ctx, lambda client: client.patch(f"/api/projects/{project}", {"state": "archived"}))


@project_app.command("activity")
def project_activity(ctx: typer.Context, project: str, limit: int = 100) -> None:
    call(ctx, lambda client: client.get("/api/activity", project_id=project, limit=limit))


s3_app = typer.Typer(no_args_is_help=True)
app.add_typer(s3_app, name="s3")


@s3_app.command("sources")
def s3_sources(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/import-sources"))


@s3_app.command("source-create")
def s3_source_create(
    ctx: typer.Context,
    name: str = typer.Option(...),
    bucket: str = typer.Option(...),
    allowed_prefix: list[str] = typer.Option(..., "--allowed-prefix"),
    endpoint_url: str | None = typer.Option(None),
    region: str | None = typer.Option(None),
    addressing_style: str = typer.Option("auto"),
    credential_env_prefix: str = typer.Option("S3"),
) -> None:
    call(
        ctx,
        lambda client: client.post(
            "/api/import-sources",
            {
                "name": name,
                "bucket": bucket,
                "allowed_prefixes": allowed_prefix,
                "endpoint_url": endpoint_url,
                "region": region,
                "addressing_style": addressing_style,
                "credential_env_prefix": credential_env_prefix,
            },
        ),
    )


@s3_app.command("source-update")
def s3_source_update(
    ctx: typer.Context,
    source: str,
    name: str | None = typer.Option(None),
    bucket: str | None = typer.Option(None),
    allowed_prefix: list[str] | None = typer.Option(None, "--allowed-prefix"),
    endpoint_url: str | None = typer.Option(None),
    region: str | None = typer.Option(None),
    addressing_style: str | None = typer.Option(None),
    credential_env_prefix: str | None = typer.Option(None),
    active: bool | None = typer.Option(None, "--active/--inactive"),
) -> None:
    values = {
        "name": name,
        "bucket": bucket,
        "allowed_prefixes": allowed_prefix,
        "endpoint_url": endpoint_url,
        "region": region,
        "addressing_style": addressing_style,
        "credential_env_prefix": credential_env_prefix,
        "is_active": active,
    }
    call(ctx, lambda client: client.patch(f"/api/import-sources/{source}", {key: value for key, value in values.items() if value is not None}))


@s3_app.command("source-test")
def s3_source_test(ctx: typer.Context, source: str) -> None:
    call(ctx, lambda client: client.post(f"/api/import-sources/{source}/test"))


@s3_app.command("ls")
def s3_ls(
    ctx: typer.Context,
    prefix: str,
    source: str = typer.Option(...),
    cursor: str | None = None,
    limit: int = 100,
) -> None:
    call(ctx, lambda client: client.get(f"/api/import-sources/{source}/browse", prefix=prefix, cursor=cursor, page_size=limit))


@s3_app.command("stat")
def s3_stat(ctx: typer.Context, key: str, source: str = typer.Option(...)) -> None:
    call(ctx, lambda client: s3_object(client, source, key))


@s3_app.command("detect")
def s3_detect(ctx: typer.Context, prefix: str, source: str = typer.Option(...)) -> None:
    call(ctx, lambda client: client.post(f"/api/import-sources/{source}/detect", {"prefix": prefix}))


@s3_app.command("tree")
def s3_tree(
    ctx: typer.Context,
    prefix: str,
    source: str = typer.Option(...),
    depth: int = typer.Option(2, min=0),
    limit: int = typer.Option(500, min=1),
) -> None:
    call(ctx, lambda client: s3_prefix_tree(client, source, prefix, depth, limit))


import_app = typer.Typer(no_args_is_help=True)
app.add_typer(import_app, name="import")


@import_app.command("preview")
def import_preview(
    ctx: typer.Context,
    prefix: str = typer.Option(...),
    source: str = typer.Option(...),
) -> None:
    call(ctx, lambda client: client.post(f"/api/import-sources/{source}/detect", {"prefix": prefix}))


@import_app.command("create")
def import_create(
    ctx: typer.Context,
    project: str = typer.Option(...),
    prefix: str = typer.Option(...),
    source: str = typer.Option(...),
    kind: str | None = typer.Option(None),
    hydrate_dataset: bool = typer.Option(True),
    dry_run: bool = typer.Option(False),
    wait: bool = typer.Option(False),
) -> None:
    payload = {
        "project_id": project,
        "source_id": source,
        "prefix": prefix,
        "kind": kind,
        "hydrate_dataset_images": hydrate_dataset,
        "dry_run": dry_run,
    }
    result = call(ctx, lambda client: client.post("/api/import-jobs", payload))
    if wait and isinstance(result, dict) and result.get("job_id"):
        wait_for_job(ctx, result["job_id"])


@import_app.command("show")
def import_show(ctx: typer.Context, import_job: str) -> None:
    call(ctx, lambda client: client.get(f"/api/import-jobs/{import_job}"))


@import_app.command("retry")
def import_retry(ctx: typer.Context, import_job: str) -> None:
    call(ctx, lambda client: client.post(f"/api/import-jobs/{import_job}/retry"))


@import_app.command("cancel")
def import_cancel(ctx: typer.Context, import_job: str) -> None:
    call(ctx, lambda client: client.post(f"/api/import-jobs/{import_job}/cancel"))


@import_app.command("refresh")
def import_refresh(ctx: typer.Context, subject: str) -> None:
    subject_type, subject_id = split_subject(subject)
    if subject_type not in {"run", "training-run", "training_run"}:
        unsupported(
            ctx,
            "import refresh",
            "Only imported training runs can currently be refreshed",
            alternative="titles run refresh <run>",
        )
        return
    call(ctx, lambda client: client.post(f"/api/runs/{subject_id}/refresh"))



@import_app.command("backfill-metrics")
def import_backfill_metrics(
    ctx: typer.Context,
    run: str | None = typer.Option(None, "--run", help="One training run ID or run:<id> subject."),
    project: str | None = typer.Option(None, "--project", help="Limit the backfill to one project."),
) -> None:
    """Re-import existing remote run metadata without hydrating large artifacts."""
    if run:
        subject_type, run_id = split_subject(run) if ":" in run else ("run", run)
        if subject_type not in {"run", "training-run", "training_run"}:
            unsupported(ctx, "import backfill-metrics", "Only training runs can be backfilled")
            return
        call(ctx, lambda client: client.post(f"/api/runs/{run_id}/refresh"))
        return

    def enqueue(client: TitlesClient) -> dict[str, Any]:
        candidates = client.get("/api/runs", project_id=project)
        queued: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in candidates if isinstance(candidates, list) else []:
            run_id = row.get("id")
            if not run_id or not row.get("source_prefix"):
                skipped.append({"run_id": run_id, "reason": "missing remote source prefix"})
                continue
            queued.append(client.post(f"/api/runs/{run_id}/refresh"))
        return {"queued": queued, "skipped": skipped}

    call(ctx, enqueue)

job_app = typer.Typer(no_args_is_help=True)
app.add_typer(job_app, name="job")


@job_app.command("list")
def job_list(ctx: typer.Context, state: str | None = None, kind: str | None = None) -> None:
    call(ctx, lambda client: client.get("/api/jobs", state=state, kind=kind))


@job_app.command("show")
def job_show(ctx: typer.Context, job: str) -> None:
    call(ctx, lambda client: client.get(f"/api/jobs/{job}"))


@job_app.command("wait")
def job_wait(ctx: typer.Context, job: str, timeout: float = 600.0) -> None:
    wait_for_job(ctx, job, timeout)


def wait_for_job(ctx: typer.Context, job: str, timeout: float = 600.0) -> None:
    runtime = rt(ctx)
    client = runtime.client()
    deadline = time.monotonic() + timeout
    try:
        while True:
            value = client.get(f"/api/jobs/{job}")
            state = value.get("state") or value.get("status")
            if not runtime.quiet and not runtime.json_output:
                typer.echo(f"{job}: {state}", err=True)
            if state in {"completed", "succeeded", "failed", "cancelled"}:
                emit(value, json_output=runtime.json_output, jsonl=runtime.jsonl)
                if state == "failed":
                    raise typer.Exit(7)
                return
            if time.monotonic() >= deadline:
                raise ApiError("Timed out waiting for job", status_code=6, code="JOB_WAIT_TIMEOUT", retryable=True)
            time.sleep(1.0)
    except ApiError as exc:
        fail(exc, json_output=runtime.json_output)
    finally:
        client.close()


@job_app.command("cancel")
def job_cancel(ctx: typer.Context, job: str) -> None:
    call(ctx, lambda client: client.post(f"/api/jobs/{job}/cancel"))


@job_app.command("logs")
def job_logs(ctx: typer.Context, job: str, tail: int = 100) -> None:
    call(ctx, lambda client: client.get(f"/api/jobs/{job}/logs", tail=tail))


dataset_app = typer.Typer(no_args_is_help=True)
app.add_typer(dataset_app, name="dataset")


@dataset_app.command("list")
def dataset_list(ctx: typer.Context, project: str | None = None) -> None:
    call(ctx, lambda client: client.get("/api/datasets", project_id=project))


@dataset_app.command("show")
def dataset_show(ctx: typer.Context, dataset: str) -> None:
    call(ctx, lambda client: client.get(f"/api/datasets/{dataset}"))


@dataset_app.command("versions")
def dataset_versions(ctx: typer.Context, dataset: str) -> None:
    call(ctx, lambda client: client.get(f"/api/datasets/{dataset}/versions"))


@dataset_app.command("items")
def dataset_items(
    ctx: typer.Context,
    version: str,
    included: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> None:
    call(ctx, lambda client: client.get(f"/api/dataset-versions/{version}/items", included=included, limit=limit, offset=offset))


@dataset_app.command("draft-create")
def dataset_draft_create(ctx: typer.Context, dataset: str, base_version: str = typer.Option(...)) -> None:
    call(ctx, lambda client: client.post(f"/api/datasets/{dataset}/drafts", {"base_version_id": base_version}))


@dataset_app.command("draft-show")
def dataset_draft_show(ctx: typer.Context, draft: str) -> None:
    call(ctx, lambda client: client.get(f"/api/dataset-drafts/{draft}"))


@dataset_app.command("draft-discard")
def dataset_draft_discard(ctx: typer.Context, draft: str) -> None:
    call(ctx, lambda client: client.delete(f"/api/dataset-drafts/{draft}"))


@dataset_app.command("publish")
def dataset_publish(ctx: typer.Context, draft: str, name: str = typer.Option(...)) -> None:
    call(ctx, lambda client: client.post(f"/api/dataset-drafts/{draft}/publish", {"name": name}))


caption_app = typer.Typer(no_args_is_help=True)
app.add_typer(caption_app, name="caption")


def caption_operation(
    ctx: typer.Context,
    draft: str,
    action: str,
    *,
    preview: bool,
    find: str | None = None,
    replace: str | None = None,
    word: str | None = None,
    item: list[str] | None = None,
    tag: str | None = None,
    included: bool | None = None,
    caption_format: str | None = None,
    all_items: bool = False,
) -> None:
    if not any([item, tag, included is not None, caption_format, all_items]):
        raise typer.BadParameter("An explicit scope is required: --item, --tag, --included, --caption-format, or --all")
    parameters = {key: value for key, value in {"find": find, "replace": replace, "word": word}.items() if value is not None}
    body = {
        "operation": action,
        "parameters": parameters,
        "item_ids": item or None,
        "tag": tag,
        "included": included,
        "caption_format": caption_format,
        "all": all_items,
    }
    suffix = "/operations/preview" if preview else "/operations"
    call(ctx, lambda client: client.post(f"/api/dataset-drafts/{draft}{suffix}", body))


@caption_app.command("replace")
def caption_replace(
    ctx: typer.Context,
    draft: str,
    find: str = typer.Option(...),
    replace: str = typer.Option(""),
    preview: bool = typer.Option(False),
    item: list[str] = typer.Option([], "--item"),
    tag: str | None = None,
    included: bool | None = None,
    caption_format: str | None = None,
    all_items: bool = typer.Option(False, "--all"),
) -> None:
    caption_operation(ctx, draft, "replace", preview=preview, find=find, replace=replace, item=item, tag=tag, included=included, caption_format=caption_format, all_items=all_items)


@caption_app.command("add-word")
def caption_add_word(
    ctx: typer.Context,
    draft: str,
    word: str = typer.Option(...),
    preview: bool = typer.Option(False),
    item: list[str] = typer.Option([], "--item"),
    tag: str | None = None,
    included: bool | None = None,
    caption_format: str | None = None,
    all_items: bool = typer.Option(False, "--all"),
) -> None:
    caption_operation(ctx, draft, "add_word", preview=preview, word=word, item=item, tag=tag, included=included, caption_format=caption_format, all_items=all_items)


@caption_app.command("set")
def caption_set(ctx: typer.Context, draft: str, item: str, text: str = typer.Option(...)) -> None:
    call(ctx, lambda client: client.patch(f"/api/dataset-drafts/{draft}/items/{item}", {"caption": text}))


run_app = typer.Typer(no_args_is_help=True)
app.add_typer(run_app, name="run")


@run_app.command("list")
def run_list(ctx: typer.Context, project: str | None = None, status: str | None = None) -> None:
    call(ctx, lambda client: client.get("/api/runs", project_id=project, status=status))


@run_app.command("show")
def run_show(ctx: typer.Context, run: str) -> None:
    call(ctx, lambda client: client.get(f"/api/runs/{run}"))


@run_app.command("refresh")
def run_refresh(ctx: typer.Context, run: str) -> None:
    call(ctx, lambda client: client.post(f"/api/runs/{run}/refresh"))


@run_app.command("config")
def run_config(ctx: typer.Context, run: str, raw: bool = False) -> None:
    call(ctx, lambda client: client.get(f"/api/runs/{run}/config", raw=raw))


@run_app.command("checkpoints")
def run_checkpoints(ctx: typer.Context, run: str) -> None:
    call(ctx, lambda client: client.get(f"/api/runs/{run}/checkpoints"))


@run_app.command("samples")
def run_samples(ctx: typer.Context, run: str, step: int | None = None, limit: int = 100) -> None:
    call(ctx, lambda client: client.get(f"/api/runs/{run}/samples", step=step, limit=limit))


@run_app.command("lineage")
def run_lineage(ctx: typer.Context, run: str) -> None:
    call(ctx, lambda client: client.get("/api/lineage", subject_type="run", subject_id=run))


checkpoint_app = typer.Typer(no_args_is_help=True)
app.add_typer(checkpoint_app, name="checkpoint")


@checkpoint_app.command("show")
def checkpoint_show(ctx: typer.Context, checkpoint: str) -> None:
    call(ctx, lambda client: client.get(f"/api/checkpoints/{checkpoint}"))


@checkpoint_app.command("download")
def checkpoint_download(ctx: typer.Context, checkpoint: str, output: Path | None = None, wait: bool = True) -> None:
    result = call(ctx, lambda client: client.post(f"/api/checkpoints/{checkpoint}/hydrate", {"output": str(output) if output else None}))
    if wait and isinstance(result, dict) and result.get("job_id"):
        wait_for_job(ctx, result["job_id"])


@checkpoint_app.command("verify")
def checkpoint_verify(ctx: typer.Context, checkpoint: str) -> None:
    unsupported(
        ctx,
        "checkpoint verify",
        "Checkpoint verification is not exposed by the current API",
        alternative=f"titles checkpoint show {checkpoint}",
    )


model_app = typer.Typer(no_args_is_help=True)
app.add_typer(model_app, name="model")


@model_app.command("list")
def model_list(ctx: typer.Context, project: str | None = None) -> None:
    call(ctx, lambda client: client.get("/api/models", project_id=project))


@model_app.command("create")
def model_create(ctx: typer.Context, project: str = typer.Option(...), name: str = typer.Option(...)) -> None:
    call(ctx, lambda client: client.post("/api/models", {"project_id": project, "name": name}))


@model_app.command("version-create")
def model_version_create(
    ctx: typer.Context,
    model: str,
    checkpoint: str = typer.Option(...),
    state: str = typer.Option("candidate"),
    name: str | None = typer.Option(None),
) -> None:
    call(ctx, lambda client: client.post("/api/model-versions", {"model_id": model, "checkpoint_id": checkpoint, "lifecycle_state": state, "name": name or f"checkpoint-{checkpoint[:8]}"}))


@model_app.command("version-show")
def model_version_show(ctx: typer.Context, version: str) -> None:
    call(ctx, lambda client: client.get(f"/api/model-versions/{version}"))


@model_app.command("version-approve")
def model_version_approve(ctx: typer.Context, version: str) -> None:
    call(ctx, lambda client: client.patch(f"/api/model-versions/{version}/state", {"lifecycle_state": "approved"}))


@model_app.command("version-archive")
def model_version_archive(ctx: typer.Context, version: str) -> None:
    call(ctx, lambda client: client.patch(f"/api/model-versions/{version}/state", {"lifecycle_state": "archived"}))


prompt_app = typer.Typer(no_args_is_help=True)
app.add_typer(prompt_app, name="prompt-set")


@prompt_app.command("list")
def prompt_list(ctx: typer.Context, project: str | None = None) -> None:
    call(ctx, lambda client: client.get("/api/prompt-sets", project_id=project))


@prompt_app.command("create")
def prompt_create(ctx: typer.Context, project: str = typer.Option(...), name: str = typer.Option(...), file: Path | None = None) -> None:
    body = {"project_id": project, "name": name}
    if file:
        body["prompts"] = read_json(file)
    call(ctx, lambda client: client.post("/api/prompt-sets", body))


@prompt_app.command("show")
def prompt_show(ctx: typer.Context, prompt_set: str) -> None:
    call(ctx, lambda client: client.get(f"/api/prompt-sets/{prompt_set}"))


@prompt_app.command("version")
def prompt_version(ctx: typer.Context, prompt_set: str, file: Path = typer.Option(...)) -> None:
    call(ctx, lambda client: client.post(f"/api/prompt-sets/{prompt_set}/versions", {"prompts": read_json(file)}))


FAL_BILLING_ACKNOWLEDGEMENT = "I understand FAL may bill this run even if checkpoint fetch fails"


def fal_eval_projection(client: TitlesClient, *, model_version: str, prompt_set: str, endpoint: str, parameters: dict[str, Any]) -> dict[str, Any]:
    version = client.get(f"/api/model-versions/{model_version}")
    project_id = str(version.get("project_id") or "")
    checkpoint_revision_id = str(version.get("checkpoint_revision_id") or "")
    if not project_id or not checkpoint_revision_id:
        raise ApiError("Model version has no immutable checkpoint revision", status_code=409, code="CHECKPOINT_REVISION_REQUIRED")
    prompt_set_row = client.get(f"/api/prompt-sets/{prompt_set}")
    prompts = [
        {"prompt_id": str(item.get("id") or f"prompt-{index}"), "prompt": str(item.get("text") or item.get("prompt") or "")}
        for index, item in enumerate(prompt_set_row.get("prompts", []))
    ]
    prompts = [item for item in prompts if item["prompt"]]
    if not prompts:
        raise ApiError("Prompt set is empty", status_code=422, code="PROMPT_SET_EMPTY")
    client.post(
        "/api/eval-definitions",
        {
            "project_id": project_id,
            "name": f"{version.get('name') or 'Model'} / {prompt_set_row.get('name') or 'Prompt set'}",
            "endpoint": endpoint,
            "model_version_id": model_version,
            "prompt_set_id": prompt_set,
            "parameters": parameters,
        },
    )
    body = {
        "kind": "eval",
        "mode": "single" if len(prompts) == 1 else "prompt_batch",
        "project_id": project_id,
        "model_version_id": model_version,
        "checkpoint_revision_id": checkpoint_revision_id,
        "prompts": prompts,
        "parameters": {**parameters, "endpoint_id": endpoint},
        "client_request_id": str(uuid.uuid4()),
    }
    return client.post("/api/fal-admissions", body)


def fal_grid_projection(client: TitlesClient, *, grid_id: str) -> dict[str, Any]:
    grid = client.get(f"/api/grids/{grid_id}")
    body = {
        "kind": "grid",
        "project_id": str(grid.get("project_id") or ""),
        "model_version_id": str(grid.get("model_version_id") or ""),
        "checkpoint_revision_id": str(grid.get("checkpoint_revision_id") or ""),
        "grid_definition_id": grid_id,
        "parameters": {**(grid.get("parameters") or {}), "endpoint_id": str(grid.get("endpoint_adapter") or "")},
        "client_request_id": str(uuid.uuid4()),
    }
    if not all(body[key] for key in ("project_id", "model_version_id", "checkpoint_revision_id")) or not body["parameters"].get("endpoint_id"):
        raise ApiError("Grid has no immutable model checkpoint revision or endpoint", status_code=409, code="CHECKPOINT_REVISION_REQUIRED")
    return client.post("/api/fal-admissions", body)


eval_app = typer.Typer(no_args_is_help=True)
app.add_typer(eval_app, name="eval")


@eval_app.command("endpoints")
def eval_endpoints(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/eval-endpoints"))


@eval_app.command("schema")
def eval_schema(ctx: typer.Context, endpoint: str) -> None:
    call(ctx, lambda client: client.get(f"/api/eval-endpoints/{endpoint}/schema"))


@eval_app.command("preview")
def eval_preview(
    ctx: typer.Context,
    model_version: str = typer.Option(...),
    prompt_set: str = typer.Option(...),
    endpoint: str = typer.Option(...),
    param: list[str] = typer.Option([], "--param"),
) -> None:
    call(ctx, lambda client: fal_eval_projection(client, model_version=model_version, prompt_set=prompt_set, endpoint=endpoint, parameters=parse_params(param)))


@eval_app.command("create")
def eval_create(
    ctx: typer.Context,
    model_version: str = typer.Option(...),
    prompt_set: str = typer.Option(...),
    endpoint: str = typer.Option(...),
    param: list[str] = typer.Option([], "--param"),
    dry_run: bool = False,
    wait: bool = False,
) -> None:
    def operation(client: TitlesClient):
        parameters = parse_params(param)
        projection = fal_eval_projection(client, model_version=model_version, prompt_set=prompt_set, endpoint=endpoint, parameters=parameters)
        if dry_run or projection.get("kind") != "projection":
            return projection
        admission = projection.get("admission") or {}
        if admission.get("state") != "ready":
            return projection
        accepted = client.post(
            f"/api/fal-admissions/{admission['id']}/admit",
            {"expected_version": admission["version"], "billing_acknowledgement": FAL_BILLING_ACKNOWLEDGEMENT},
        )
        if accepted.get("eval_run_id"):
            run = client.get(f"/api/eval-runs/{accepted['eval_run_id']}")
            accepted["job_id"] = run.get("job_id")
        return accepted

    result = call(ctx, operation)
    if wait and isinstance(result, dict) and result.get("job_id"):
        wait_for_job(ctx, result["job_id"])


@eval_app.command("show")
def eval_show(ctx: typer.Context, eval_run: str) -> None:
    call(ctx, lambda client: client.get(f"/api/eval-runs/{eval_run}"))


@eval_app.command("outputs")
def eval_outputs(ctx: typer.Context, eval_run: str) -> None:
    call(ctx, lambda client: client.get(f"/api/eval-runs/{eval_run}/outputs"))


@eval_app.command("cancel")
def eval_cancel(ctx: typer.Context, eval_run: str) -> None:
    call(ctx, lambda client: client.post(f"/api/eval-runs/{eval_run}/cancel"))


@eval_app.command("lineage")
def eval_lineage(ctx: typer.Context, eval_run: str) -> None:
    call(ctx, lambda client: client.get("/api/lineage", subject_type="eval_run", subject_id=eval_run))


grid_app = typer.Typer(no_args_is_help=True)
app.add_typer(grid_app, name="grid")


@grid_app.command("create")
def grid_create(
    ctx: typer.Context,
    model_version: str = typer.Option(...),
    prompt_set: str = typer.Option(...),
    endpoint: str = typer.Option(...),
    x_axis: str = typer.Option(...),
    x_value: list[str] = typer.Option([], "--x-value"),
    y_axis: str = typer.Option(...),
    y_value: list[str] = typer.Option([], "--y-value"),
    param: list[str] = typer.Option([], "--param"),
) -> None:
    call(ctx, lambda client: client.post("/api/grids", {"model_version_id": model_version, "prompt_set_id": prompt_set, "endpoint": endpoint, "x_axis": {"field": x_axis, "values": x_value}, "y_axis": {"field": y_axis, "values": y_value}, "parameters": parse_params(param)}))


@grid_app.command("show")
def grid_show(ctx: typer.Context, grid: str) -> None:
    call(ctx, lambda client: client.get(f"/api/grids/{grid}"))


@grid_app.command("run")
def grid_run(ctx: typer.Context, grid: str, dry_run: bool = False, wait: bool = False) -> None:
    def operation(client: TitlesClient):
        projection = fal_grid_projection(client, grid_id=grid)
        if dry_run or projection.get("kind") != "projection":
            return projection
        admission = projection.get("admission") or {}
        if admission.get("state") != "ready":
            return projection
        accepted = client.post(
            f"/api/fal-admissions/{admission['id']}/admit",
            {"expected_version": admission["version"], "billing_acknowledgement": FAL_BILLING_ACKNOWLEDGEMENT},
        )
        if accepted.get("eval_run_id"):
            run = client.get(f"/api/eval-runs/{accepted['eval_run_id']}")
            accepted["job_id"] = run.get("job_id")
        return accepted

    result = call(ctx, operation)
    if wait and isinstance(result, dict) and result.get("job_id"):
        wait_for_job(ctx, result["job_id"])


@grid_app.command("cells")
def grid_cells(ctx: typer.Context, grid_run: str) -> None:
    call(ctx, lambda client: client.get(f"/api/grid-runs/{grid_run}/cells"))


gallery_app = typer.Typer(no_args_is_help=True)
app.add_typer(gallery_app, name="gallery")


@gallery_app.command("list")
def gallery_list(
    ctx: typer.Context,
    project: str | None = None,
    kind: str | None = None,
    query: str | None = typer.Option(None, "--query", "-q"),
    decision: str | None = None,
    rating: int | None = typer.Option(None, min=1, max=5),
    limit: int = 100,
    offset: int = 0,
) -> None:
    call(ctx, lambda client: client.get("/api/gallery", project_id=project, kind=kind, q=query, decision=decision, rating=rating, limit=limit, offset=offset))


asset_app = typer.Typer(no_args_is_help=True)
app.add_typer(asset_app, name="asset")


@asset_app.command("show")
def asset_show(ctx: typer.Context, asset: str) -> None:
    call(ctx, lambda client: client.get(f"/api/assets/{asset}"))


@asset_app.command("locations")
def asset_locations(ctx: typer.Context, asset: str) -> None:
    call(ctx, lambda client: client.get(f"/api/assets/{asset}/locations"))


@asset_app.command("metadata")
def asset_metadata(ctx: typer.Context, asset: str) -> None:
    call(ctx, lambda client: client.get(f"/api/assets/{asset}/metadata"))


@asset_app.command("lineage")
def asset_lineage(ctx: typer.Context, asset: str) -> None:
    call(ctx, lambda client: client.get("/api/lineage", subject_type="asset", subject_id=asset))


review_app = typer.Typer(no_args_is_help=True)
app.add_typer(review_app, name="review")


@review_app.command("set")
def review_set(
    ctx: typer.Context,
    subject: str,
    rating: int | None = typer.Option(None, min=1, max=5),
    decision: str | None = typer.Option(None),
) -> None:
    subject_type, subject_id = split_subject(subject)
    call(ctx, lambda client: client.post("/api/reviews", {"subject_type": subject_type, "subject_id": subject_id, "rating": rating, "decision": decision}))


@review_app.command("show")
def review_show(ctx: typer.Context, subject: str) -> None:
    subject_type, subject_id = split_subject(subject)
    call(ctx, lambda client: client.get("/api/reviews", subject_type=subject_type, subject_id=subject_id))


@review_app.command("list")
def review_list(ctx: typer.Context, project: str | None = None, decision: str | None = None, profile: str | None = None) -> None:
    call(ctx, lambda client: client.get("/api/reviews", project_id=project, decision=decision, profile_id=profile))


comment_app = typer.Typer(no_args_is_help=True)
app.add_typer(comment_app, name="comment")


@comment_app.command("list")
def comment_list(ctx: typer.Context, subject: str) -> None:
    subject_type, subject_id = split_subject(subject)
    call(ctx, lambda client: client.get("/api/comments", subject_type=subject_type, subject_id=subject_id))


@comment_app.command("add")
def comment_add(ctx: typer.Context, subject: str, text: str = typer.Option(...)) -> None:
    subject_type, subject_id = split_subject(subject)
    call(ctx, lambda client: client.post("/api/comments", {"subject_type": subject_type, "subject_id": subject_id, "body": text}))


@comment_app.command("update")
def comment_update(ctx: typer.Context, comment: str, text: str = typer.Option(...)) -> None:
    unsupported(
        ctx,
        "comment update",
        "Comments are immutable in the current API",
        alternative="titles comment add <subject> --text <text>",
    )


note_app = typer.Typer(no_args_is_help=True)
app.add_typer(note_app, name="note")


@note_app.command("list")
def note_list(ctx: typer.Context, subject: str) -> None:
    subject_type, subject_id = split_subject(subject)
    call(ctx, lambda client: client.get("/api/notes", subject_type=subject_type, subject_id=subject_id))


@note_app.command("add")
def note_add(ctx: typer.Context, subject: str, file: Path = typer.Option(...)) -> None:
    subject_type, subject_id = split_subject(subject)
    call(ctx, lambda client: client.post("/api/notes", {"subject_type": subject_type, "subject_id": subject_id, "body": file.read_text(encoding="utf-8")}))


activity_app = typer.Typer(no_args_is_help=True)
app.add_typer(activity_app, name="activity")


@activity_app.command("list")
def activity_list(ctx: typer.Context, project: str | None = None, profile: str | None = None, since: str | None = None, limit: int = 100) -> None:
    call(ctx, lambda client: client.get("/api/activity", project_id=project, profile_id=profile, since=since, limit=limit))


lineage_app = typer.Typer(no_args_is_help=True)
app.add_typer(lineage_app, name="lineage")


@lineage_app.command("show")
def lineage_show(ctx: typer.Context, subject: str) -> None:
    subject_type, subject_id = split_subject(subject)
    call(ctx, lambda client: client.get("/api/lineage", subject_type=subject_type, subject_id=subject_id))


@lineage_app.command("path")
def lineage_path(ctx: typer.Context, source: str, target: str) -> None:
    unsupported(
        ctx,
        "lineage path",
        "Lineage path traversal is not exposed by the current API",
        alternative=f"titles lineage show {source}",
    )


connection_app = typer.Typer(no_args_is_help=True)
app.add_typer(connection_app, name="connection")


@connection_app.command("list")
def connection_list(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/connections"))


@connection_app.command("test")
def connection_test(ctx: typer.Context, provider: str) -> None:
    call(ctx, lambda client: client.post(f"/api/connections/{provider}/test"))


storage_app = typer.Typer(no_args_is_help=True)
app.add_typer(storage_app, name="storage")


def storage_database_path(value: Path | None) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    return (project_root() / "var" / "titles-dam.sqlite3").resolve()


@storage_app.command("verify-contract")
def storage_verify_contract(
    ctx: typer.Context,
    database: Path | None = typer.Option(None, "--database"),
    run_lease_id: str | None = typer.Option(None, "--run-lease-id", envvar="TITLES_RUN_LEASE_ID"),
) -> None:
    if not run_lease_id:
        raise typer.BadParameter("--run-lease-id or TITLES_RUN_LEASE_ID is required")
    from titles_api.storage.gates import GateEvidence, StorageGateStore

    path = storage_database_path(database)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        version = db.execute("SELECT version_num FROM alembic_version").fetchone()
        marker = db.execute(
            "SELECT marker,binding_digest FROM storage_cutover_marker WHERE id=1"
        ).fetchone()
        if version is None or marker is None or int(marker["marker"]) != 1:
            raise typer.BadParameter("database head/marker is not ready for verification")
        payload = f"{version['version_num']}:{marker['marker']}:{marker['binding_digest']}:1"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        projection = StorageGateStore(db).verify_storage_contract(
            GateEvidence(
                run_lease_id=run_lease_id,
                alembic_head=str(version["version_num"]),
                marker=int(marker["marker"]),
                binding_digest=str(marker["binding_digest"]),
                contract_version=1,
                evidence_digest=digest,
            ),
            expected_run_lease_id=run_lease_id,
        )
    emit(
        {
            "name": projection.name,
            "state": projection.state.value,
            "version": projection.version,
            "verification_receipt_id": projection.verification_receipt_id,
        },
        json_output=rt(ctx).json_output,
    )


@storage_app.command("recover-cutover")
def storage_recover_cutover(
    ctx: typer.Context,
    backup_id: str = typer.Argument(...),
    kind: str = typer.Option(..., "--kind", help="raw or sanitized"),
    database: Path | None = typer.Option(None, "--database"),
    confirm: str | None = typer.Option(None, "--confirm"),
) -> None:
    from titles_api.storage.backup import EnvelopeStore, FileKeyStore
    from titles_api.storage.cutover import RestoreEligibility, restore_raw, restore_sanitized

    if kind not in {"raw", "sanitized"}:
        raise typer.BadParameter("--kind must be raw or sanitized")
    root_path = project_root() / "var" / "cutover"
    store = EnvelopeStore(root_path / "backups", FileKeyStore(root_path / "keys"))
    path = storage_database_path(database)
    if kind == "raw":
        receipt = restore_raw(envelope_store=store, backup_id=backup_id, database_path=path)
    else:
        with sqlite3.connect(path) as db:
            gates = {
                str(name): str(state)
                for name, state in db.execute("SELECT name,state FROM storage_feature_gates")
            }
            marker = db.execute("SELECT effect_epoch FROM storage_cutover_marker WHERE id=1").fetchone()
            if marker is None:
                raise typer.BadParameter("cutover marker is missing")
            effect_rows = 0
            table_names = {
                str(row[0])
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in ("publication_receipts", "fal_submission_intents"):
                if table in table_names:
                    effect_rows += int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        receipt = restore_sanitized(
            envelope_store=store,
            backup_id=backup_id,
            database_path=path,
            eligibility=RestoreEligibility(
                gate_states=gates,
                effect_epoch=int(marker[0]),
                effect_rows=effect_rows,
            ),
            confirmation=confirm or "",
        )
    emit(
        {"backup_id": receipt.backup_id, "kind": receipt.kind, "sha256": receipt.sha256, "restored": str(path)},
        json_output=rt(ctx).json_output,
    )


server_app = typer.Typer(no_args_is_help=True)
app.add_typer(server_app, name="server")


def project_root() -> Path:
    configured = os.getenv("TITLES_PROJECT_ROOT")
    if configured:
        return Path(configured).resolve()
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "IMPLEMENTATION_PLAN.md").exists() and (candidate / "apps").exists():
            return candidate
    return Path(__file__).resolve().parents[4]

def _pid_is_alive(path: Path) -> tuple[int | None, bool]:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None, False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return pid, False
    return pid, True


def _url_is_ready(url: str, *, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def _terminate_pid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _service_pid_paths(var: Path) -> dict[str, Path]:
    return {
        "supervisor": var / "supervisor.pid",
        "api": var / "server.pid",
        "worker": var / "worker.pid",
        "web": var / "web.pid",
    }


def _stack_state(root_path: Path) -> dict[str, Any]:
    var = root_path / "var"
    paths = _service_pid_paths(var)
    api_port = int(os.environ.get("TITLES_API_PORT", "8400"))
    web_port = int(os.environ.get("TITLES_WEB_PORT", "5173"))
    services: dict[str, dict[str, Any]] = {}
    for name, path in paths.items():
        pid, alive = _pid_is_alive(path)
        healthy = alive
        if name == "api":
            healthy = alive and _url_is_ready(f"http://127.0.0.1:{api_port}/api/health")
        elif name == "web":
            healthy = alive and _url_is_ready(f"http://127.0.0.1:{web_port}/")
        services[name] = {"pid": pid, "running": alive, "healthy": healthy}
    ready = (var / "server.ready").exists()
    ok = ready and all(service["healthy"] for service in services.values())
    return {
        "ok": ok,
        "running": any(service["running"] for service in services.values()),
        "ready": ready,
        "services": services,
        "api_url": f"http://127.0.0.1:{api_port}",
        "web_url": f"http://127.0.0.1:{web_port}",
    }


@server_app.command("start")
def server_start(ctx: typer.Context) -> None:
    root_path = project_root()
    var = root_path / "var"
    var.mkdir(parents=True, exist_ok=True)
    pid_paths = _service_pid_paths(var)
    for service, pid_path in pid_paths.items():
        if not pid_path.exists():
            continue
        _pid, alive = _pid_is_alive(pid_path)
        if alive:
            raise typer.BadParameter(f"{service} is already running")
        pid_path.unlink(missing_ok=True)
    (var / "server.ready").unlink(missing_ok=True)
    (var / "server.error").unlink(missing_ok=True)
    pythonpath = os.pathsep.join(str(root_path / "apps" / name) for name in ("api", "worker", "cli"))
    env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "TITLES_PROJECT_ROOT": str(root_path),
        "TITLES_RUN_LEASE_ID": str(uuid.uuid4()),
        "TITLES_RUN_LOCK": str(var / "titles-dam.run.lock"),
    }
    supervisor_log = (var / "supervisor.log").open("ab")
    try:
        supervisor = subprocess.Popen(
            [sys.executable, "-m", "titles_cli.server_supervisor"],
            cwd=root_path,
            env=env,
            stdout=supervisor_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        supervisor_log.close()
    pid_paths["supervisor"].write_text(str(supervisor.pid), encoding="utf-8")
    timeout = float(os.environ.get("TITLES_SERVER_START_TIMEOUT", "20"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (var / "server.ready").exists():
            state = _stack_state(root_path)
            emit(state, json_output=rt(ctx).json_output)
            return
        if supervisor.poll() is not None:
            detail = (var / "server.error").read_text(encoding="utf-8") if (var / "server.error").exists() else "service supervisor exited during startup"
            raise typer.BadParameter(detail)
        time.sleep(0.1)
    _terminate_pid(supervisor.pid)
    raise typer.BadParameter(f"service stack did not become ready within {timeout:g}s")


@server_app.command("status")
def server_status(ctx: typer.Context) -> None:
    state = _stack_state(project_root())
    emit(state, json_output=rt(ctx).json_output)
    if not state["ok"]:
        raise typer.Exit(code=1)


@server_app.command("stop")
def server_stop(ctx: typer.Context) -> None:
    var = project_root() / "var"
    pid_paths = _service_pid_paths(var)
    pids: list[int] = []
    for name in ("supervisor", "web", "worker", "api"):
        path = pid_paths[name]
        pid, alive = _pid_is_alive(path)
        if pid is not None:
            pids.append(pid)
        if alive and pid is not None:
            _terminate_pid(pid)
        path.unlink(missing_ok=True)
    (var / "server.ready").unlink(missing_ok=True)
    (var / "server.error").unlink(missing_ok=True)
    emit({"ok": True, "running": False, "pids": pids}, json_output=rt(ctx).json_output)


if __name__ == "__main__":
    app()
