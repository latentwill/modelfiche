from __future__ import annotations

import base64
import io
import json
import hashlib
import mimetypes
import os
import signal
import shutil
import subprocess
import sqlite3
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
import urllib.error
from urllib.parse import urlsplit
import urllib.request
from pathlib import Path
from typing import Any, Callable

import typer
import yaml

from . import __version__
from .client import ApiError, LocalContext, TitlesClient, context_path
from .render import emit, fail
from .wandb_helpers import (
    generate_keypair,
    public_key_for_private,
    read_private_key,
    render_agent_environment,
    sign_launch_claims,
    write_private_key,
    write_secret_text,
)


app = typer.Typer(
    name="mfiche", no_args_is_help=True, help="Modelfiche operator and agent CLI"
)


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
    ctx.obj = Runtime(
        local, profile, workspace, request_id, timeout, json_output, jsonl, quiet
    )


def rt(ctx: typer.Context) -> Runtime:
    return ctx.find_root().obj


def call(
    ctx: typer.Context,
    operation: Callable[[TitlesClient], Any],
    *,
    emit_result: bool = True,
) -> Any:
    runtime = rt(ctx)
    client: TitlesClient | None = None
    try:
        client = runtime.client()
        result = operation(client)
        if emit_result and (
            runtime.json_output or runtime.jsonl or not runtime.quiet
        ):
            emit(result, json_output=runtime.json_output, jsonl=runtime.jsonl)
        return result
    except ApiError as exc:
        fail(exc, json_output=runtime.json_output)
    finally:
        if client is not None:
            client.close()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"Cannot read JSON from {path}: {exc}") from exc


def parse_json_option(value: str, option: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{option} must be valid JSON") from exc


def raw_call(ctx: typer.Context, operation: Callable[[TitlesClient], Any]) -> Any:
    runtime = rt(ctx)
    client: TitlesClient | None = None
    try:
        client = runtime.client()
        return operation(client)
    except ApiError as exc:
        fail(exc, json_output=runtime.json_output)
    finally:
        if client is not None:
            client.close()



def emit_raw(
    ctx: typer.Context, payload: bytes | str, output: Path | None, *, kind: str
) -> None:
    runtime = rt(ctx)
    if output is not None:
        destination = output.expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            destination.write_bytes(payload)
            size = len(payload)
        else:
            destination.write_text(payload, encoding="utf-8")
            size = len(payload.encode("utf-8"))
        emit(
            {"output": str(destination), "bytes": size, "kind": kind},
            json_output=runtime.json_output,
            jsonl=runtime.jsonl,
        )
        return
    if runtime.json_output:
        if isinstance(payload, bytes) and kind == "manifest":
            try:
                emit(json.loads(payload), json_output=True, jsonl=runtime.jsonl)
                return
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        emit(
            {
                "content": payload.decode("utf-8")
                if isinstance(payload, bytes)
                else payload
            },
            json_output=True,
            jsonl=runtime.jsonl,
        )
        return
    if isinstance(payload, bytes):
        typer.echo(payload.decode("utf-8"), nl=False)
    else:
        typer.echo(payload, nl=False)


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
        raise typer.BadParameter(
            "Subject must use typed form, for example asset:<uuid>"
        )
    subject_type, subject_id = subject.split(":", 1)
    if not subject_type or not subject_id:
        raise typer.BadParameter("Subject type and ID are required")
    return subject_type, subject_id


def unsupported(
    ctx: typer.Context, command: str, reason: str, *, alternative: str | None = None
) -> None:
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


def s3_prefix_tree(
    client: TitlesClient, source: str, prefix: str, depth: int, limit: int
) -> dict[str, Any]:
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
    client: TitlesClient | None = None
    try:
        client = runtime.client()
        payload["server"] = client.get("/api/system/version")
    except ApiError as exc:
        if exc.code != "SERVICE_UNAVAILABLE":
            fail(exc, json_output=runtime.json_output)
        payload["server"] = None
        emit(payload, json_output=runtime.json_output)
        raise typer.Exit(8)
    finally:
        if client is not None:
            client.close()
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


workspace_app = typer.Typer(no_args_is_help=True)
app.add_typer(workspace_app, name="workspace")


@workspace_app.command("list")
def workspace_list(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/workspaces"))


@workspace_app.command("rename")
def workspace_rename(
    ctx: typer.Context, workspace: str, name: str = typer.Option(...)
) -> None:
    call(
        ctx, lambda client: client.patch(f"/api/workspaces/{workspace}", {"name": name})
    )


profile_app = typer.Typer(no_args_is_help=True)
app.add_typer(profile_app, name="profile")


@profile_app.command("list")
def profile_list(ctx: typer.Context, include_archived: bool = False) -> None:
    call(
        ctx,
        lambda client: client.get("/api/profiles", include_archived=include_archived),
    )


@profile_app.command("create")
def profile_create(
    ctx: typer.Context,
    name: str = typer.Option(...),
    email: str | None = typer.Option(None),
    color: str | None = typer.Option(None),
) -> None:
    call(
        ctx,
        lambda client: client.post(
            "/api/profiles",
            {"display_name": name, "email": email, "avatar_color": color},
        ),
    )


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
    call(
        ctx,
        lambda client: client.patch(
            f"/api/profiles/{profile}",
            {"display_name": name, "email": email, "avatar_color": color},
        ),
    )


@profile_app.command("archive")
def profile_archive(ctx: typer.Context, profile: str) -> None:
    call(ctx, lambda client: client.post(f"/api/profiles/{profile}/archive"))


project_app = typer.Typer(no_args_is_help=True)
app.add_typer(project_app, name="project")


@project_app.command("list")
def project_list(
    ctx: typer.Context, state: str | None = typer.Option(None, "--state")
) -> None:
    call(ctx, lambda client: client.get("/api/projects", state=state))


@project_app.command("create")
def project_create(
    ctx: typer.Context,
    name: str = typer.Option(...),
    description: str | None = typer.Option(None),
    trigger: list[str] = typer.Option([], "--trigger"),
) -> None:
    call(
        ctx,
        lambda client: client.post(
            "/api/projects",
            {"title": name, "description": description, "trigger_words": trigger},
        ),
    )


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
    call(
        ctx,
        lambda client: client.patch(
            f"/api/projects/{project}",
            {"title": name, "description": description, "state": state},
        ),
    )


@project_app.command("archive")
def project_archive(ctx: typer.Context, project: str) -> None:
    call(
        ctx,
        lambda client: client.patch(f"/api/projects/{project}", {"state": "archived"}),
    )


@project_app.command("activity")
def project_activity(ctx: typer.Context, project: str, limit: int = 100) -> None:
    call(
        ctx, lambda client: client.get("/api/activity", project_id=project, limit=limit)
    )


wandb_app = typer.Typer(no_args_is_help=True)
app.add_typer(wandb_app, name="wandb")

wandb_key_app = typer.Typer(no_args_is_help=True)
wandb_app.add_typer(wandb_key_app, name="key")


@wandb_key_app.command("generate")
def wandb_key_generate(
    ctx: typer.Context,
    private_key_output: Path = typer.Option(..., "--private-key-output"),
) -> None:
    """Generate a local signing key without sending private material to the DAM."""
    private_key, public_key = generate_keypair()
    try:
        destination = write_private_key(private_key_output, private_key)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--private-key-output") from exc
    emit(
        {
            "algorithm": "Ed25519",
            "public_key": public_key,
            "private_key_path": str(destination),
        },
        json_output=rt(ctx).json_output,
        jsonl=rt(ctx).jsonl,
    )


@wandb_key_app.command("public")
def wandb_key_public(
    ctx: typer.Context,
    private_key_file: Path = typer.Option(..., "--private-key-file"),
) -> None:
    """Derive the registerable public key from a local signing key."""
    try:
        private_key = read_private_key(private_key_file)
        public_key = public_key_for_private(private_key)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--private-key-file") from exc
    emit(
        {"algorithm": "Ed25519", "public_key": public_key},
        json_output=rt(ctx).json_output,
        jsonl=rt(ctx).jsonl,
    )


wandb_credential_app = typer.Typer(
    no_args_is_help=True, help="Manage the one global W&B signing key"
)
wandb_app.add_typer(wandb_credential_app, name="credential")
wandb_app.add_typer(wandb_credential_app, name="credentials")


def _global_wandb_credential(client: TitlesClient) -> dict[str, Any]:
    credentials = client.get("/api/integrations/wandb/credentials")
    if not isinstance(credentials, list) or not credentials:
        raise ApiError(
            "The global W&B signing key is not authenticated; run `mfiche training setup`",
            status_code=4,
            code="TRAINING_SIGNING_NOT_AUTHENTICATED",
        )
    return credentials[0]


@wandb_credential_app.command("register")
def wandb_credential_register(
    ctx: typer.Context,
    public_key: str = typer.Option(..., "--public-key"),
) -> None:
    """Register or reconcile the global W&B public key."""
    body = {"alias": "modelfiche", "public_key": public_key}
    call(ctx, lambda client: client.post("/api/integrations/wandb/credentials", body))


@wandb_credential_app.command("list")
def wandb_credential_list(ctx: typer.Context) -> None:
    """Show the global W&B signing key."""
    call(ctx, lambda client: client.get("/api/integrations/wandb/credentials"))


@wandb_credential_app.command("rotate")
def wandb_credential_rotate(
    ctx: typer.Context,
    public_key: str = typer.Option(..., "--public-key"),
) -> None:
    """Replace the global W&B public key."""
    def rotate(client: TitlesClient) -> Any:
        credential = _global_wandb_credential(client)
        return client.post(
            f"/api/integrations/wandb/credentials/{credential['id']}/rotate",
            {"public_key": public_key},
        )

    call(ctx, rotate)


@wandb_credential_app.command("revoke")
def wandb_credential_revoke(ctx: typer.Context) -> None:
    """Revoke the global W&B public key."""
    def revoke(client: TitlesClient) -> Any:
        credential = _global_wandb_credential(client)
        return client.post(
            f"/api/integrations/wandb/credentials/{credential['id']}/revoke"
        )

    call(ctx, revoke)


training_launch_app = typer.Typer(no_args_is_help=True)
app.add_typer(training_launch_app, name="training-launch")
app.add_typer(training_launch_app, name="training-launches")


def training_launch_payload(
    *,
    input_file: Path | None,
    dataset_version_id: str | None,
    source_id: str | None,
    client_request_id: str | None,
    name: str | None,
    trainer: str | None,
    base_model: str | None,
    output_directory: str | None,
    checkpoint_policy: str | None,
    backup_policy: str | None,
    supported_endpoint_ids: list[str],
    supported_endpoint_ids_json: str | None,
    expected_duration_seconds: int | None,
) -> dict[str, Any]:
    payload = read_json(input_file) if input_file is not None else {}
    if not isinstance(payload, dict):
        raise typer.BadParameter("--input must contain a JSON object")
    forbidden = {
        "private_key",
        "password_manager",
        "password_manager_locator",
        "password_manager_name",
    }
    present_forbidden = sorted(forbidden.intersection(payload))
    if present_forbidden:
        raise typer.BadParameter(
            f"--input contains forbidden secret fields: {', '.join(present_forbidden)}"
        )
    allowed = {
        "dataset_version_id",
        "source_id",
        "client_request_id",
        "name",
        "trainer",
        "base_model",
        "output_directory",
        "checkpoint_policy",
        "backup_policy",
        "supported_endpoint_ids",
        "expected_duration_seconds",
    }
    payload = {key: value for key, value in payload.items() if key in allowed}

    direct_values = {
        "dataset_version_id": dataset_version_id,
        "source_id": source_id,
        "client_request_id": client_request_id,
        "name": name,
        "trainer": trainer,
        "base_model": base_model,
        "output_directory": output_directory,
        "expected_duration_seconds": expected_duration_seconds,
    }
    payload.update(
        {key: value for key, value in direct_values.items() if value is not None}
    )
    if checkpoint_policy is not None:
        value = parse_json_option(checkpoint_policy, "--checkpoint-policy")
        if not isinstance(value, dict):
            raise typer.BadParameter("--checkpoint-policy must be a JSON object")
        payload["checkpoint_policy"] = value
    if backup_policy is not None:
        value = parse_json_option(backup_policy, "--backup-policy")
        if not isinstance(value, dict):
            raise typer.BadParameter("--backup-policy must be a JSON object")
        payload["backup_policy"] = value
    if supported_endpoint_ids_json is not None:
        value = parse_json_option(
            supported_endpoint_ids_json, "--supported-endpoint-ids"
        )
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise typer.BadParameter(
                "--supported-endpoint-ids must be a JSON array of strings"
            )
        payload["supported_endpoint_ids"] = value
    elif supported_endpoint_ids:
        payload["supported_endpoint_ids"] = supported_endpoint_ids
    payload.setdefault("trainer", "ai-toolkit")
    payload.setdefault("checkpoint_policy", {})
    payload.setdefault("backup_policy", {})
    payload.setdefault("supported_endpoint_ids", [])
    payload.setdefault("expected_duration_seconds", 604800)

    required = (
        "dataset_version_id",
        "source_id",
        "client_request_id",
        "name",
        "base_model",
        "output_directory",
    )
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise typer.BadParameter(f"Missing launch fields: {', '.join(missing)}")
    return payload


@training_launch_app.command("create")
def training_launch_create(
    ctx: typer.Context,
    input_file: Path | None = typer.Option(None, "--input", "--input-file", "--file"),
    dataset_version_id: str | None = typer.Option(None, "--dataset-version-id"),
    source_id: str | None = typer.Option(None, "--source-id"),
    client_request_id: str | None = typer.Option(None, "--client-request-id"),
    name: str | None = typer.Option(None, "--name"),
    trainer: str | None = typer.Option(None, "--trainer"),
    base_model: str | None = typer.Option(None, "--base-model"),
    output_directory: str | None = typer.Option(None, "--output-directory"),
    checkpoint_policy: str | None = typer.Option(None, "--checkpoint-policy"),
    backup_policy: str | None = typer.Option(None, "--backup-policy"),
    supported_endpoint_ids: list[str] = typer.Option([], "--supported-endpoint-id"),
    supported_endpoint_ids_json: str | None = typer.Option(
        None, "--supported-endpoint-ids"
    ),
    expected_duration_seconds: int | None = typer.Option(
        None, "--expected-duration-seconds"
    ),
) -> None:
    payload = training_launch_payload(
        input_file=input_file,
        dataset_version_id=dataset_version_id,
        source_id=source_id,
        client_request_id=client_request_id,
        name=name,
        trainer=trainer,
        base_model=base_model,
        output_directory=output_directory,
        checkpoint_policy=checkpoint_policy,
        backup_policy=backup_policy,
        supported_endpoint_ids=supported_endpoint_ids,
        supported_endpoint_ids_json=supported_endpoint_ids_json,
        expected_duration_seconds=expected_duration_seconds,
    )
    call(ctx, lambda client: client.post("/api/training-launches", payload))


@training_launch_app.command("show")
@training_launch_app.command("status")
def training_launch_status(ctx: typer.Context, launch: str) -> None:
    call(ctx, lambda client: client.get(f"/api/training-launches/{launch}"))


def training_launch_manifest(
    ctx: typer.Context,
    launch: str,
    output: Path | None = typer.Option(None, "--output", "--output-file", "-o"),
) -> None:
    payload = raw_call(
        ctx,
        lambda client: client.get_bytes(f"/api/training-launches/{launch}/manifest"),
    )
    emit_raw(ctx, payload, output, kind="manifest")


training_launch_app.command("manifest")(training_launch_manifest)


def training_launch_environment(
    ctx: typer.Context,
    launch: str,
    output: Path | None = typer.Option(None, "--output", "--output-file", "-o"),
) -> None:
    payload = raw_call(
        ctx,
        lambda client: client.get_text(f"/api/training-launches/{launch}/environment"),
    )
    emit_raw(ctx, payload, output, kind="environment")


training_launch_app.command("environment")(training_launch_environment)


@training_launch_app.command("inspect")
def training_launch_inspect(ctx: typer.Context, launch: str) -> None:
    """Return the launch contract and current live run projection together."""

    def projection(client: TitlesClient) -> dict[str, Any]:
        launch_value = client.get(f"/api/training-launches/{launch}")
        run_id = launch_value.get("run_id") if isinstance(launch_value, dict) else None
        if not isinstance(run_id, str) or not run_id:
            raise ApiError(
                "Training launch response is missing run_id",
                status_code=5,
                code="TRAINING_LAUNCH_RUN_MISSING",
                retryable=False,
            )
        return {
            "launch": launch_value,
            "run": client.get(f"/api/runs/{run_id}/live"),
        }

    call(ctx, projection)


@training_launch_app.command("agent-environment")
def training_launch_agent_environment(
    ctx: typer.Context,
    launch: str,
    private_key_file: Path = typer.Option(..., "--private-key-file"),
    output: Path | None = typer.Option(None, "--output", "--output-file", "-o"),
) -> None:
    """Render a ready-to-source environment while keeping the private key local."""

    def inputs(client: TitlesClient) -> tuple[Any, str]:
        return (
            client.get(f"/api/training-launches/{launch}"),
            client.get_text(f"/api/training-launches/{launch}/environment"),
        )

    launch_value, template = raw_call(ctx, inputs)
    try:
        manifest = (
            launch_value.get("manifest") if isinstance(launch_value, dict) else None
        )
        telemetry = manifest.get("telemetry") if isinstance(manifest, dict) else None
        token = telemetry.get("token") if isinstance(telemetry, dict) else None
        claims = token.get("claims") if isinstance(token, dict) else None
        if not isinstance(claims, dict):
            raise ValueError(
                "training launch manifest is missing telemetry token claims"
            )
        private_key = read_private_key(private_key_file)
        environment = render_agent_environment(
            template, sign_launch_claims(claims, private_key)
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if output is None:
        emit_raw(ctx, environment, None, kind="agent-environment")
        return
    try:
        destination = write_secret_text(output, environment)
    except OSError as exc:
        raise typer.BadParameter(
            f"cannot write agent environment to {output}: {exc}", param_hint="--output"
        ) from exc
    emit(
        {
            "launch_id": launch,
            "run_id": launch_value.get("run_id"),
            "output": str(destination),
            "bytes": len(environment.encode("utf-8")),
            "mode": "0600",
            "kind": "agent-environment",
        },
        json_output=rt(ctx).json_output,
        jsonl=rt(ctx).jsonl,
    )


@training_launch_app.command("wait")
def training_launch_wait(
    ctx: typer.Context,
    launch: str,
    timeout: float = typer.Option(604800.0, min=0.1),
    poll_interval: float = typer.Option(2.0, "--poll-interval", min=0.1, max=60.0),
) -> None:
    """Wait for completion, failure, or heartbeat interruption."""
    runtime = rt(ctx)
    client = runtime.client()
    deadline = time.monotonic() + timeout
    try:
        while True:
            value = client.get(f"/api/training-launches/{launch}")
            state = value.get("state") if isinstance(value, dict) else None
            if not runtime.quiet and not runtime.json_output:
                typer.echo(f"{launch}: {state}", err=True)
            if state in {"completed", "failed", "interrupted"}:
                emit(value, json_output=runtime.json_output, jsonl=runtime.jsonl)
                if state != "completed":
                    raise typer.Exit(7)
                return
            if time.monotonic() >= deadline:
                raise ApiError(
                    "Timed out waiting for training launch",
                    status_code=6,
                    code="TRAINING_LAUNCH_WAIT_TIMEOUT",
                    retryable=True,
                )
            time.sleep(poll_interval)
    except ApiError as exc:
        fail(exc, json_output=runtime.json_output)
    finally:
        client.close()


training_app = typer.Typer(
    no_args_is_help=True, help="Guided training setup, preflight, and launch"
)
training_run_app = typer.Typer(no_args_is_help=True)
app.add_typer(training_app, name="training")
training_app.add_typer(training_run_app, name="run")


def _training_profile_path() -> Path:
    return context_path().parent / "training.json"


def _load_training_profile() -> dict[str, Any]:
    path = _training_profile_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_training_profile(value: dict[str, Any]) -> Path:
    path = _training_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)
    return path


@training_app.command("doctor")
def training_doctor(ctx: typer.Context) -> None:
    """Return one actionable readiness report for training setup."""
    call(ctx, lambda client: client.get("/api/training-setup"))


@training_app.command("setup")
def training_setup(
    ctx: typer.Context,
    private_key_file: Path = typer.Option(
        Path.home() / ".config" / "modelfiche" / "ai-toolkit-signing.key",
        "--private-key-file",
    ),
) -> None:
    """Create or reconcile the one global signing identity."""
    destination = private_key_file.expanduser()
    if destination.exists():
        private_key = read_private_key(destination)
        public_key = public_key_for_private(private_key)
        generated = False
    else:
        private_key, public_key = generate_keypair()
        destination = write_private_key(destination, private_key)
        generated = True

    runtime = rt(ctx)
    client = runtime.client()
    try:
        credentials = client.get("/api/integrations/wandb/credentials")
        if credentials:
            credential = credentials[0]
            if credential.get("public_key") != public_key:
                credential = client.post(
                    f"/api/integrations/wandb/credentials/{credential['id']}/rotate",
                    {"public_key": public_key},
                )
        else:
            credential = client.post(
                "/api/integrations/wandb/credentials",
                {"alias": "modelfiche", "public_key": public_key},
            )
        profile_path = _save_training_profile(
            {"private_key_path": str(destination)}
        )
        readiness = client.get("/api/training-setup")
        emit(
            {
                "configured": True,
                "generated_private_key": generated,
                "private_key_path": str(destination),
                "credential_alias": credential["alias"],
                "credential_id": credential["id"],
                "key_id": credential["key_id"],
                "profile_path": str(profile_path),
                "readiness": readiness,
            },
            json_output=runtime.json_output,
            jsonl=runtime.jsonl,
        )
    except ApiError as exc:
        fail(exc, json_output=runtime.json_output)
    finally:
        client.close()


def _resolve_dataset_version(
    client: TitlesClient, dataset_value: str, version_value: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve an explicit dataset ID without relying on a paginated listing."""
    try:
        uuid.UUID(dataset_value)
    except ValueError:
        datasets = client.get("/api/datasets", limit=250)
        matches = [
            item
            for item in datasets
            if str(item.get("name", "")).casefold() == dataset_value.casefold()
        ]
        if len(matches) != 1:
            raise ApiError(
                f"Dataset {dataset_value!r} did not resolve uniquely",
                status_code=4,
                code="TRAINING_DATASET_AMBIGUOUS",
                details={
                    "matches": [
                        {"id": item.get("id"), "name": item.get("name")}
                        for item in matches
                    ]
                },
            )
        dataset = matches[0]
    else:
        dataset = client.get(f"/api/datasets/{dataset_value}")
    versions = client.get(f"/api/datasets/{dataset['id']}/versions")
    if version_value is None:
        version_id = dataset.get("current_version_id")
        selected = next(
            (item for item in versions if item.get("id") == version_id), None
        )
    else:
        selected = next(
            (
                item
                for item in versions
                if item.get("id") == version_value
                or str(item.get("version_number")) == version_value
                or str(item.get("name", "")).casefold() == version_value.casefold()
            ),
            None,
        )
    if selected is None:
        raise ApiError(
            f"Published version {version_value or 'current'} was not found",
            status_code=4,
            code="TRAINING_DATASET_VERSION_NOT_FOUND",
        )
    return dataset, selected


@training_run_app.command("create")
def training_run_create(
    ctx: typer.Context,
    dataset: str = typer.Option(..., "--dataset"),
    name: str = typer.Option(..., "--name"),
    base_model: str = typer.Option(..., "--base-model"),
    trainer: str = typer.Option("ai-toolkit", "--trainer"),
    source: str | None = typer.Option(None, "--source"),
    version: str | None = typer.Option(None, "--version"),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Disable live W&B graphs/samples; retain checkpoints and W&B files in object storage.",
    ),
    output_directory: str = typer.Option("/workspace/output", "--output-directory"),
    checkpoint_every: int = typer.Option(1000, "--checkpoint-every", min=1),
    backup_every: int = typer.Option(300, "--backup-every", min=30, max=3600),
    max_duration: int = typer.Option(
        86400, "--max-duration-seconds", min=300, max=2592000
    ),
    steps: int = typer.Option(8000, "--steps", min=1),
    tokens: int = typer.Option(5, "--tokens", min=1, max=128),
    learning_rate: float = typer.Option(5e-4, "--learning-rate", "--lr", min=0),
    batch_size: int = typer.Option(1, "--batch-size", min=1),
    gradient_accumulation: int = typer.Option(
        1, "--gradient-accumulation", min=1
    ),
    resolution: int = typer.Option(512, "--resolution", min=64, max=4096),
    max_sequence_length: int = typer.Option(
        512, "--max-sequence-length", min=1, max=4096
    ),
    seed: int = typer.Option(42, "--seed", min=0),
    log_interval: int = typer.Option(10, "--log-interval", min=1),
    sample_interval: int | None = typer.Option(None, "--sample-interval", min=1),
    sample_steps: int = typer.Option(28, "--sample-steps", min=1),
    sample_resolution: int = typer.Option(
        512, "--sample-resolution", min=64, max=4096
    ),
    sample_text_guidance: float = typer.Option(3.5, "--sample-text-guidance", min=0),
    concept_type: str = typer.Option("concept", "--concept-type"),
    dtype: str = typer.Option("bf16", "--dtype"),
    transformer_storage_dtype: str = typer.Option(
        "fp8", "--transformer-storage-dtype"
    ),
    gradient_checkpointing: bool = typer.Option(
        True, "--gradient-checkpointing/--no-gradient-checkpointing"
    ),
    low_vram: bool = typer.Option(False, "--low-vram/--no-low-vram"),
    compile_transformer: bool = typer.Option(
        True, "--compile-transformer/--no-compile-transformer"
    ),
    preload_cache: bool = typer.Option(
        True, "--preload-cache/--no-preload-cache"
    ),
    timestep_distribution: str = typer.Option(
        "shifted_logit_normal", "--timestep-distribution"
    ),
    timestep_mu: float = typer.Option(0.0, "--timestep-mu"),
    timestep_sigma: float = typer.Option(1.0, "--timestep-sigma", min=0),
    resolution_shift: bool = typer.Option(
        True, "--resolution-shift/--no-resolution-shift"
    ),
    base_image_seq_len: int = typer.Option(256, "--base-image-seq-len", min=1),
    max_image_seq_len: int = typer.Option(6400, "--max-image-seq-len", min=1),
    base_shift: float = typer.Option(0.5, "--base-shift"),
    max_shift: float = typer.Option(1.15, "--max-shift"),
    caption_extension: str = typer.Option(".txt", "--caption-extension"),
    caption_mode: str = typer.Option("paired", "--caption-mode"),
    rebuild_cache: bool = typer.Option(False, "--rebuild-cache/--reuse-cache"),
    model_revision: str | None = typer.Option(None, "--model-revision"),
) -> None:
    """Create a launch from human-readable inputs using verified setup defaults."""
    if trainer not in {"ai-toolkit", "kef-krea2"}:
        raise typer.BadParameter("--trainer must be ai-toolkit or kef-krea2")
    runtime = rt(ctx)
    client = runtime.client()
    try:
        dataset_row, version_row = _resolve_dataset_version(client, dataset, version)
        setup = client.get("/api/training-setup")
        sources = [
            item
            for item in setup.get("sources", [])
            if item.get("available") or item.get("verified")
        ]
        if not setup.get("credentials"):
            raise ApiError(
                "The global W&B signing key is not authenticated; run `mfiche training setup`",
                status_code=4,
                code="TRAINING_SIGNING_NOT_AUTHENTICATED",
            )
        selected_sources = (
            [
                item
                for item in sources
                if source in {str(item.get("id", "")), str(item.get("name", ""))}
            ]
            if source
            else sources
        )
        if len(selected_sources) != 1:
            if source:
                raise ApiError(
                    f"Training storage source {source!r} was not uniquely available",
                    status_code=4,
                    code="TRAINING_STORAGE_NOT_RESOLVED",
                    details={"sources": sources},
                )
            raise ApiError(
                "Exactly one verified training storage source is required for guided launch; pass --source",
                status_code=4,
                code="TRAINING_STORAGE_NOT_RESOLVED",
                details={"sources": sources},
            )
        trainer_config = (
            {
                "steps": steps,
                "num_tokens": tokens,
                "learning_rate": learning_rate,
                "batch_size": batch_size,
                "gradient_accumulation": gradient_accumulation,
                "resolution": resolution,
                "max_sequence_length": max_sequence_length,
                "seed": seed,
                "checkpoint_interval": checkpoint_every,
                "log_interval": log_interval,
                "sample_interval": sample_interval,
                "sample_steps": sample_steps,
                "sample_resolution": sample_resolution,
                "sample_text_guidance": sample_text_guidance,
                "concept_type": concept_type,
                "dtype": dtype,
                "transformer_storage_dtype": transformer_storage_dtype,
                "gradient_checkpointing": gradient_checkpointing,
                "low_vram": low_vram,
                "compile_transformer": compile_transformer,
                "preload_cache": preload_cache,
                "timestep_distribution": timestep_distribution,
                "timestep_mu": timestep_mu,
                "timestep_sigma": timestep_sigma,
                "resolution_shift": resolution_shift,
                "base_image_seq_len": base_image_seq_len,
                "max_image_seq_len": max_image_seq_len,
                "base_shift": base_shift,
                "max_shift": max_shift,
                "caption_extension": caption_extension,
                "caption_mode": caption_mode,
                "rebuild_cache": rebuild_cache,
                "model_revision": model_revision,
            }
            if trainer == "kef-krea2"
            else None
        )
        request_id = rt(ctx).request_id or f"training-{uuid.uuid4()}"
        launch = client.post(
            "/api/training-launches",
            {
                "dataset_version_id": version_row["id"],
                "source_id": selected_sources[0]["id"],
                "client_request_id": request_id,
                "name": name,
                "trainer": trainer,
                "base_model": base_model,
                "output_directory": output_directory,
                "live_telemetry": False if offline else None,
                "training_config": trainer_config,
                "checkpoint_policy": {"every_n_steps": checkpoint_every},
                "backup_policy": {"interval_seconds": backup_every},
                "supported_endpoint_ids": [],
                "expected_duration_seconds": max_duration,
            },
        )
        emit(
            {
                "launch": launch,
                "dataset": {"id": dataset_row["id"], "name": dataset_row.get("name")},
                "version": {
                    "id": version_row["id"],
                    "name": version_row.get("name"),
                    "number": version_row.get("version_number"),
                },
                "next_action": f"mfiche training run preflight {launch['id']}",
            },
            json_output=runtime.json_output,
            jsonl=runtime.jsonl,
        )
    except ApiError as exc:
        fail(exc, json_output=runtime.json_output)
    finally:
        client.close()


@training_run_app.command("preflight")
def training_run_preflight(ctx: typer.Context, launch: str) -> None:
    call(ctx, lambda client: client.get(f"/api/training-launches/{launch}/preflight"))


def _prepare_kef_krea2_dataset(archive_bytes: bytes, *, concept_type: str) -> bytes:
    """Convert a ModelFiche export into kef-krea2's immutable dataset layout."""
    source_buffer = io.BytesIO(archive_bytes)
    target_buffer = io.BytesIO()
    with zipfile.ZipFile(source_buffer) as source:
        try:
            manifest = json.loads(source.read("manifest.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError("ModelFiche dataset export is missing a valid manifest.json") from exc
        assets = {
            str(asset["id"]): asset
            for asset in manifest.get("assets", [])
            if isinstance(asset, dict) and asset.get("id")
        }
        items = sorted(
            (
                item
                for item in manifest.get("dataset_items", [])
                if isinstance(item, dict) and item.get("included", True)
            ),
            key=lambda item: (int(item.get("position", 0)), str(item.get("id", ""))),
        )
        if not items:
            raise ValueError("ModelFiche dataset export contains no included dataset items")

        records: list[dict[str, Any]] = []
        with zipfile.ZipFile(
            target_buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as target:
            for item in items:
                asset_id = str(item.get("asset_id", ""))
                asset = assets.get(asset_id)
                if asset is None:
                    raise ValueError(f"Dataset item {item.get('id')} references an unknown asset")
                candidates = [
                    name
                    for name in source.namelist()
                    if name.startswith(f"files/{asset_id}/") and not name.endswith("/")
                ]
                if len(candidates) != 1:
                    raise ValueError(
                        f"Dataset asset {asset_id} must have exactly one hydrated file; found {len(candidates)}"
                    )
                filename = Path(str(asset.get("name") or Path(candidates[0]).name)).name
                relative_path = f"images/{asset_id}/{filename}"
                image_bytes = source.read(candidates[0])
                target.writestr(relative_path, image_bytes)
                record: dict[str, Any] = {
                    "path": relative_path,
                    "caption": str(item.get("caption") or "").strip(),
                    "sha256": hashlib.sha256(image_bytes).hexdigest(),
                    "split": "train",
                    "source_type": "modelfiche",
                    "provenance": {
                        "asset_id": asset_id,
                        "dataset_item_id": str(item.get("id", "")),
                        "original_caption": str(item.get("caption") or ""),
                    },
                }
                if concept_type == "style":
                    record["caption_role"] = "content"
                records.append(record)
            target.writestr(
                "manifest.final.jsonl",
                "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            )
    return target_buffer.getvalue()


@training_run_app.command("prepare")
def training_run_prepare(
    ctx: typer.Context,
    launch: str,
    output: Path = typer.Option(..., "--output", "-o"),
    private_key_file: Path | None = typer.Option(None, "--private-key-file"),
) -> None:
    """Write a checksummed, ready-to-run packet only after preflight passes."""
    profile = _load_training_profile()
    key_path = (
        private_key_file or Path(str(profile.get("private_key_path", "")))
    ).expanduser()
    if not str(key_path) or not key_path.is_file():
        raise typer.BadParameter(
            "Run `mfiche training setup` or provide --private-key-file"
        )
    runtime = rt(ctx)
    client = runtime.client()
    try:
        packet = client.get(f"/api/training-launches/{launch}/packet")
        template = client.get_text(f"/api/training-launches/{launch}/environment")
        claims = packet["manifest"]["telemetry"]["token"]["claims"]
        secret_environment = render_agent_environment(
            template, sign_launch_claims(claims, read_private_key(key_path))
        )
        # Dataset exports belong to the operator API, even for older manifests
        # that incorrectly used the separate W&B ingress origin.
        dataset_path = urlsplit(str(packet["manifest"]["dataset"]["download_url"])).path
        dataset_archive = client.get_bytes(dataset_path)
        destination = output.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        destination.chmod(0o700)
        trainer_id = str(packet["manifest"]["run"]["trainer"])
        if trainer_id == "kef-krea2":
            dataset_archive = _prepare_kef_krea2_dataset(
                dataset_archive,
                concept_type=str(
                    packet["trainer"]["configuration"].get("concept_type", "concept")
                ),
            )
        if trainer_id == "ai-toolkit":
            trainer_files = {
                "ai-toolkit-logging.yaml": yaml.safe_dump(
                    packet["ai_toolkit"], sort_keys=False
                ),
            }
            readme = (
                "ModelFiche AI Toolkit training packet\n\n"
                "For live telemetry install wandb==0.28.0 in the trainer environment.\n\n"
                "1. Copy this directory to the trainer host over an encrypted channel.\n"
                "2. Verify checksums.json, then extract training-dataset.zip.\n"
                "3. Provide the S3 credentials named in manifest.json.\n"
                "4. Enable packet W&B settings by running `set -a; . ./trainer.env; set +a`.\n"
                "5. Merge ai-toolkit-logging.yaml into the selected AI Toolkit job config.\n"
                "6. Start `python3 modelfiche-run-sync.py --manifest manifest.json` beside AI Toolkit.\n"
                "7. Start AI Toolkit. Online mode sends losses/samples to the configured W&B ingress; "
                "offline mode backs up checkpoints/logs/samples only, without live ingestion.\n"
            )
            next_action = (
                "Copy the packet, verify checksums, export trainer.env, and start AI Toolkit "
                "with outbound S3 credentials. Online mode provides live graphs/samples; "
                "offline mode provides archival files only."
            )
        elif trainer_id == "kef-krea2":
            trainer_files = {
                "kef-krea2-telemetry.json": json.dumps(
                    packet["trainer"]["telemetry"], indent=2, sort_keys=True
                )
                + "\n",
                "kef-krea2-training.json": json.dumps(
                    {
                        "configuration": packet["trainer"]["configuration"],
                        "command": packet["trainer"]["command"],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            }
            readme = (
                "ModelFiche kef-krea2 training packet\n\n"
                "For live telemetry install wandb==0.28.0 in the trainer environment.\n\n"
                "1. Copy this directory to the trainer host over an encrypted channel.\n"
                "2. Verify checksums.json, then run `mkdir training-dataset && "
                "unzip training-dataset.zip -d training-dataset`.\n"
                "3. Provide the S3 credentials named in manifest.json.\n"
                "4. Enable packet W&B settings by running `set -a; . ./trainer.env; set +a` "
                "in the kef-krea2 trainer process.\n"
                "5. Review kef-krea2-training.json; its command argv preserves every launch setting.\n"
                "6. Start `python3 modelfiche-run-sync.py --manifest manifest.json` beside kef-krea2.\n"
                "7. Start the command recorded in kef-krea2-training.json exactly once. "
                "Online mode sends losses/samples to configured ingress; offline mode backs up files only.\n"
            )
            next_action = (
                "Copy the packet, verify checksums, export trainer.env, and start one kef-krea2 run "
                "with outbound S3 credentials. Online mode provides live graphs/samples; "
                "offline mode provides archival files only."
            )
        else:
            raise ValueError(f"unsupported trainer packet: {trainer_id}")
        files = {
            "manifest.json": json.dumps(packet["manifest"], indent=2, sort_keys=True)
            + "\n",
            "preflight.json": json.dumps(packet["preflight"], indent=2, sort_keys=True)
            + "\n",
            "trainer.env": secret_environment,
            "modelfiche-run-sync.py": Path(__file__)
            .with_name("run_sync.py")
            .read_text(encoding="utf-8"),
            "run-sync-command.txt": "python3 modelfiche-run-sync.py --manifest manifest.json\n",
            "README.txt": readme,
            **trainer_files,
        }
        checksums: dict[str, str] = {}
        for filename, body in files.items():
            path = destination / filename
            path.write_text(body, encoding="utf-8")
            path.chmod(0o600 if filename == "trainer.env" else 0o644)
            checksums[filename] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        dataset_path = destination / "training-dataset.zip"
        dataset_path.write_bytes(dataset_archive)
        dataset_path.chmod(0o644)
        checksums["training-dataset.zip"] = hashlib.sha256(dataset_archive).hexdigest()
        checksum_body = (
            json.dumps(
                {"algorithm": "sha256", "files": checksums}, indent=2, sort_keys=True
            )
            + "\n"
        )
        (destination / "checksums.json").write_text(checksum_body, encoding="utf-8")
        emit(
            {
                "ready": True,
                "launch_id": launch,
                "run_id": packet["run_id"],
                "output": str(destination),
                "files": sorted([*files, "training-dataset.zip", "checksums.json"]),
                "next_action": next_action,
            },
            json_output=runtime.json_output,
            jsonl=runtime.jsonl,
        )
    except (ApiError, ValueError) as exc:
        if isinstance(exc, ApiError):
            fail(exc, json_output=runtime.json_output)
        raise typer.BadParameter(str(exc)) from exc
    finally:
        client.close()


@training_run_app.command("cancel")
def training_run_cancel(ctx: typer.Context, launch: str) -> None:
    call(ctx, lambda client: client.post(f"/api/training-launches/{launch}/cancel"))


@training_run_app.command("delete")
def training_run_delete(ctx: typer.Context, launch: str) -> None:
    call(ctx, lambda client: client.delete(f"/api/training-launches/{launch}"))


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
    call(
        ctx,
        lambda client: client.patch(
            f"/api/import-sources/{source}",
            {key: value for key, value in values.items() if value is not None},
        ),
    )


@s3_app.command("source-test")
def s3_source_test(ctx: typer.Context, source: str) -> None:
    call(ctx, lambda client: client.post(f"/api/import-sources/{source}/test"))


@s3_app.command("source-disconnect")
def s3_source_disconnect(ctx: typer.Context, source: str) -> None:
    """Disable a source without deleting bucket data."""
    call(ctx, lambda client: client.delete(f"/api/import-sources/{source}"))


@s3_app.command("ls")
def s3_ls(
    ctx: typer.Context,
    prefix: str,
    source: str = typer.Option(...),
    cursor: str | None = None,
    limit: int = 100,
) -> None:
    call(
        ctx,
        lambda client: client.get(
            f"/api/import-sources/{source}/browse",
            prefix=prefix,
            cursor=cursor,
            page_size=limit,
        ),
    )


@s3_app.command("stat")
def s3_stat(ctx: typer.Context, key: str, source: str = typer.Option(...)) -> None:
    call(ctx, lambda client: s3_object(client, source, key))


@s3_app.command("detect")
def s3_detect(ctx: typer.Context, prefix: str, source: str = typer.Option(...)) -> None:
    call(
        ctx,
        lambda client: client.post(
            f"/api/import-sources/{source}/detect", {"prefix": prefix}
        ),
    )


@s3_app.command("tree")
def s3_tree(
    ctx: typer.Context,
    prefix: str,
    source: str = typer.Option(...),
    depth: int = typer.Option(2, min=0),
    limit: int = typer.Option(500, min=1),
) -> None:
    call(ctx, lambda client: s3_prefix_tree(client, source, prefix, depth, limit))


transfer_app = typer.Typer(no_args_is_help=True)
app.add_typer(transfer_app, name="transfer")


def transfer_payload(
    *,
    project: str | None,
    assets: list[str],
    dataset_versions: list[str],
    model_versions: list[str],
    eval_runs: list[str],
    include_files: bool,
    include_reviews: bool,
    review_token: str | None = None,
) -> dict[str, Any]:
    return {
        "project_id": project,
        "asset_ids": assets,
        "dataset_version_ids": dataset_versions,
        "model_version_ids": model_versions,
        "eval_run_ids": eval_runs,
        "include_files": include_files,
        "include_reviews": include_reviews,
        "review_token": review_token,
    }


@transfer_app.command("preview")
def transfer_preview(
    ctx: typer.Context,
    project: str | None = typer.Option(None),
    asset: list[str] = typer.Option([], "--asset"),
    dataset_version: list[str] = typer.Option([], "--dataset-version"),
    model_version: list[str] = typer.Option([], "--model-version"),
    eval_run: list[str] = typer.Option([], "--eval-run"),
    include_files: bool = typer.Option(True, "--include-files/--exclude-files"),
    include_reviews: bool = typer.Option(True, "--include-reviews/--exclude-reviews"),
) -> None:
    payload = transfer_payload(
        project=project,
        assets=asset,
        dataset_versions=dataset_version,
        model_versions=model_version,
        eval_runs=eval_run,
        include_files=include_files,
        include_reviews=include_reviews,
    )
    payload.pop("review_token")
    call(ctx, lambda client: client.post("/api/transfers/preview", payload))


@transfer_app.command("create")
def transfer_create(
    ctx: typer.Context,
    review_token: str = typer.Option(
        ..., help="Token returned by `mfiche transfer preview`"
    ),
    project: str | None = typer.Option(None),
    asset: list[str] = typer.Option([], "--asset"),
    dataset_version: list[str] = typer.Option([], "--dataset-version"),
    model_version: list[str] = typer.Option([], "--model-version"),
    eval_run: list[str] = typer.Option([], "--eval-run"),
    include_files: bool = typer.Option(True, "--include-files/--exclude-files"),
    include_reviews: bool = typer.Option(True, "--include-reviews/--exclude-reviews"),
) -> None:
    payload = transfer_payload(
        project=project,
        assets=asset,
        dataset_versions=dataset_version,
        model_versions=model_version,
        eval_runs=eval_run,
        include_files=include_files,
        include_reviews=include_reviews,
        review_token=review_token,
    )
    call(ctx, lambda client: client.post("/api/transfers", payload))


@transfer_app.command("list")
def transfer_list(ctx: typer.Context, state: str | None = typer.Option(None)) -> None:
    call(ctx, lambda client: client.get("/api/transfers", state=state))


@transfer_app.command("show")
def transfer_show(ctx: typer.Context, transfer: str) -> None:
    call(ctx, lambda client: client.get(f"/api/transfers/{transfer}"))


@transfer_app.command("cancel")
def transfer_cancel(ctx: typer.Context, transfer: str) -> None:
    call(ctx, lambda client: client.post(f"/api/transfers/{transfer}/cancel"))


import_app = typer.Typer(no_args_is_help=True)
app.add_typer(import_app, name="import")


@import_app.command("preview")
def import_preview(
    ctx: typer.Context,
    prefix: str = typer.Option(...),
    source: str = typer.Option(...),
) -> None:
    call(
        ctx,
        lambda client: client.post(
            f"/api/import-sources/{source}/detect", {"prefix": prefix}
        ),
    )


@import_app.command("create")
def import_create(
    ctx: typer.Context,
    project: str = typer.Option(...),
    prefix: str = typer.Option(...),
    source: str = typer.Option(...),
    kind: str | None = typer.Option(None),
    hydrate_dataset: bool = typer.Option(True),
    dry_run: bool = typer.Option(False),
    review_token: str | None = typer.Option(
        None, help="Token returned by `mfiche import preview`; required for mutation"
    ),
    wait: bool = typer.Option(False),
) -> None:
    payload = {
        "project_id": project,
        "source_id": source,
        "prefix": prefix,
        "kind": kind,
        "hydrate_dataset_images": hydrate_dataset,
        "dry_run": dry_run,
        "review_token": review_token,
    }
    result = call(
        ctx,
        lambda client: client.post("/api/import-jobs", payload),
        emit_result=not wait,
    )
    if wait and isinstance(result, dict) and result.get("job_id"):
        wait_for_job(ctx, result["job_id"])
    elif wait:
        emit(result, json_output=rt(ctx).json_output, jsonl=rt(ctx).jsonl)


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
            alternative="mfiche run refresh <run>",
        )
        return
    call(ctx, lambda client: client.post(f"/api/runs/{subject_id}/refresh"))


@import_app.command("backfill-metrics")
def import_backfill_metrics(
    ctx: typer.Context,
    run: str | None = typer.Option(
        None, "--run", help="One training run ID or run:<id> subject."
    ),
    project: str | None = typer.Option(
        None, "--project", help="Limit the backfill to one project."
    ),
) -> None:
    """Re-import existing remote run metadata without hydrating large artifacts."""
    if run:
        subject_type, run_id = split_subject(run) if ":" in run else ("run", run)
        if subject_type not in {"run", "training-run", "training_run"}:
            unsupported(
                ctx, "import backfill-metrics", "Only training runs can be backfilled"
            )
            return
        call(ctx, lambda client: client.post(f"/api/runs/{run_id}/refresh"))
        return

    def enqueue(client: TitlesClient) -> dict[str, Any]:
        queued: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"project_id": project, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = client.get("/api/runs", **params)
            if isinstance(page, list):
                rows = page
                next_cursor = None
            elif isinstance(page, dict):
                rows = next(
                    (
                        page.get(key)
                        for key in ("items", "results", "data")
                        if isinstance(page.get(key), list)
                    ),
                    [],
                )
                next_cursor = page.get("next_cursor")
            else:
                rows = []
                next_cursor = None
            for row in rows:
                run_id = row.get("id")
                if not run_id or not row.get("source_prefix"):
                    skipped.append(
                        {"run_id": run_id, "reason": "missing remote source prefix"}
                    )
                    continue
                queued.append(client.post(f"/api/runs/{run_id}/refresh"))
            if not next_cursor:
                break
            cursor = str(next_cursor)
        return {"queued": queued, "skipped": skipped}

    call(ctx, enqueue)


@import_app.command("local")
def import_local(
    ctx: typer.Context,
    folder: Path = typer.Argument(..., exists=True, file_okay=False, readable=True),
    project: str = typer.Option(...),
    name: str = typer.Option(..., help="Dataset name"),
) -> None:
    supported = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".avif",
        ".txt",
        ".json",
    }
    paths = sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in supported
    )
    if not paths:
        raise typer.BadParameter("Folder contains no supported images or sidecars")
    if len(paths) > 1000:
        raise typer.BadParameter("Folder contains more than the 1000-file import limit")
    total_size = sum(path.stat().st_size for path in paths)
    if total_size > 250 * 1024 * 1024:
        raise typer.BadParameter("Folder exceeds the 250 MiB import limit")
    runtime = rt(ctx)
    if not runtime.quiet and not runtime.json_output:
        typer.echo(
            f"Uploading {len(paths)} files ({total_size} bytes) from {folder}",
            err=True,
        )
    files = [
        {
            "path": path.relative_to(folder).as_posix(),
            "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            "mime_type": mimetypes.guess_type(path.name)[0],
            "last_modified": int(path.stat().st_mtime * 1000),
        }
        for path in paths
    ]
    call(
        ctx,
        lambda client: client.post(
            "/api/local-imports",
            {"project_id": project, "dataset_name": name, "files": files},
        ),
    )


job_app = typer.Typer(no_args_is_help=True)
app.add_typer(job_app, name="job")


@job_app.command("list")
def job_list(
    ctx: typer.Context, state: str | None = None, kind: str | None = None
) -> None:
    call(ctx, lambda client: client.get("/api/jobs", state=state, kind=kind))


@job_app.command("show")
def job_show(ctx: typer.Context, job: str) -> None:
    call(ctx, lambda client: client.get(f"/api/jobs/{job}"))


@job_app.command("wait")
def job_wait(ctx: typer.Context, job: str, timeout: float = 600.0) -> None:
    wait_for_job(ctx, job, timeout)


def wait_for_job(ctx: typer.Context, job: str, timeout: float = 600.0) -> None:
    runtime = rt(ctx)
    client: TitlesClient | None = None
    deadline = time.monotonic() + timeout
    try:
        client = runtime.client()
        while True:
            value = client.get(f"/api/jobs/{job}")
            state = value.get("state") or value.get("status")
            if not runtime.quiet and not runtime.json_output:
                typer.echo(f"{job}: {state}", err=True)
            if state in {"succeeded", "failed", "canceled"}:
                emit(value, json_output=runtime.json_output, jsonl=runtime.jsonl)
                if state == "failed":
                    raise typer.Exit(7)
                return
            if time.monotonic() >= deadline:
                raise ApiError(
                    "Timed out waiting for job",
                    status_code=6,
                    code="JOB_WAIT_TIMEOUT",
                    retryable=True,
                )
            time.sleep(1.0)
    except ApiError as exc:
        fail(exc, json_output=runtime.json_output)
    finally:
        if client is not None:
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
    call(
        ctx,
        lambda client: client.get(
            f"/api/dataset-versions/{version}/items",
            included=included,
            limit=limit,
            offset=offset,
        ),
    )


@dataset_app.command("draft-create")
def dataset_draft_create(
    ctx: typer.Context, dataset: str, base_version: str = typer.Option(...)
) -> None:
    call(
        ctx,
        lambda client: client.post(
            f"/api/datasets/{dataset}/drafts", {"base_version_id": base_version}
        ),
    )


@dataset_app.command("draft-show")
def dataset_draft_show(ctx: typer.Context, draft: str) -> None:
    call(ctx, lambda client: client.get(f"/api/dataset-drafts/{draft}"))


@dataset_app.command("draft-discard")
def dataset_draft_discard(ctx: typer.Context, draft: str) -> None:
    call(ctx, lambda client: client.delete(f"/api/dataset-drafts/{draft}"))


@dataset_app.command("publish")
def dataset_publish(
    ctx: typer.Context, draft: str, name: str = typer.Option(...)
) -> None:
    call(
        ctx,
        lambda client: client.post(
            f"/api/dataset-drafts/{draft}/publish", {"name": name}
        ),
    )
captions_app = typer.Typer(no_args_is_help=True)
dataset_app.add_typer(captions_app, name="captions")
caption_app = typer.Typer(no_args_is_help=True)
app.add_typer(caption_app, name="caption")



_CAPTION_OPERATIONS = {"replace", "add-word", "remove-word"}


def _parse_where_included(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise typer.BadParameter("--where-included must be true or false")


def _caption_scope(
    *,
    item: list[str],
    where_tag: str | None,
    where_included: str | None,
    where_caption_format: str | None,
    all_items: bool,
) -> dict[str, Any]:
    if not any(
        [
            item,
            where_tag,
            where_included is not None,
            where_caption_format,
            all_items,
        ]
    ):
        raise typer.BadParameter(
            "An explicit scope is required: --item, --where-tag, "
            "--where-included, --where-caption-format, or --all"
        )
    if where_caption_format is not None and where_caption_format not in {"text", "json"}:
        raise typer.BadParameter("--where-caption-format must be text or json")
    return {
        "item_ids": item or None,
        "tag": where_tag,
        "included": _parse_where_included(where_included),
        "caption_format": where_caption_format,
        "all": all_items,
    }


def _caption_change_set(
    ctx: typer.Context,
    draft: str,
    operation: str,
    *,
    apply: bool,
    find: str | None,
    replace: str | None,
    word: str | None,
    item: list[str],
    where_tag: str | None,
    where_included: str | None,
    where_caption_format: str | None,
    all_items: bool,
) -> None:
    if operation not in _CAPTION_OPERATIONS:
        choices = ", ".join(sorted(_CAPTION_OPERATIONS))
        raise typer.BadParameter(f"operation must be one of: {choices}")
    if operation == "replace" and find is None:
        raise typer.BadParameter("--find is required for replace")
    if operation in {"add-word", "remove-word"} and word is None:
        raise typer.BadParameter("--word is required for add-word or remove-word")
    if operation == "replace" and replace is None:
        raise typer.BadParameter("--replace is required for replace")
    scope = _caption_scope(
        item=item,
        where_tag=where_tag,
        where_included=where_included,
        where_caption_format=where_caption_format,
        all_items=all_items,
    )
    parameters = {
        key: value
        for key, value in {
            "find": find,
            "replace": replace if replace is not None else "",
            "word": word,
        }.items()
        if value is not None
    }
    body = {"operation": operation.replace("-", "_"), "parameters": parameters, **scope}

    def request(client: TitlesClient) -> Any:
        preview = client.post(
            f"/api/dataset-drafts/{draft}/operations/preview",
            body,
        )
        if not apply:
            return preview
        preview_token = preview.get("preview_token") if isinstance(preview, dict) else None
        if not preview_token:
            raise typer.BadParameter(
                "The API did not return a preview token; apply was not performed"
            )
        return client.post(
            f"/api/dataset-drafts/{draft}/operations",
            {**body, "preview_token": preview_token},
        )

    result = call(ctx, request, emit_result=False)
    runtime = rt(ctx)
    if runtime.json_output or runtime.jsonl:
        emit(result, json_output=runtime.json_output, jsonl=runtime.jsonl)
        return
    matched = result.get("matched", 0) if isinstance(result, dict) else 0
    changes = result.get("changes", []) if isinstance(result, dict) else []
    verb = "Applied" if apply else "Proposed"
    typer.echo(f"{verb} changes for {matched} matched item(s); {len(changes)} change(s).")
    if not changes:
        typer.echo("No changes.")
        return
    for change in changes:
        item_id = change.get("item_id", "<unknown>")
        field = change.get("field", "caption")
        before = change.get("before")
        after = change.get("after")
        typer.echo(f"{item_id} ({field})")
        typer.echo(f"- {before}")
        typer.echo(f"+ {after}")


def _caption_change_command(
    ctx: typer.Context,
    draft: str,
    operation: str,
    *,
    apply: bool,
    find: str | None,
    replace: str | None,
    word: str | None,
    item: list[str],
    where_tag: str | None,
    where_included: str | None,
    where_caption_format: str | None,
    all_items: bool,
) -> None:
    _caption_change_set(
        ctx,
        draft,
        operation,
        apply=apply,
        find=find,
        replace=replace,
        word=word,
        item=item,
        where_tag=where_tag,
        where_included=where_included,
        where_caption_format=where_caption_format,
        all_items=all_items,
    )


def _caption_preview_options(
    ctx: typer.Context,
    draft: str,
    operation: str,
    find: str | None = typer.Option(None, "--find"),
    replace: str | None = typer.Option(None, "--replace"),
    word: str | None = typer.Option(None, "--word"),
    item: list[str] = typer.Option([], "--item"),
    where_tag: str | None = typer.Option(None, "--where-tag"),
    where_included: str | None = typer.Option(None, "--where-included"),
    where_caption_format: str | None = typer.Option(None, "--where-caption-format"),
    all_items: bool = typer.Option(False, "--all"),
) -> None:
    _caption_change_command(
        ctx,
        draft,
        operation,
        apply=False,
        find=find,
        replace=replace,
        word=word,
        item=item,
        where_tag=where_tag,
        where_included=where_included,
        where_caption_format=where_caption_format,
        all_items=all_items,
    )


def _caption_apply_options(
    ctx: typer.Context,
    draft: str,
    operation: str,
    find: str | None = typer.Option(None, "--find"),
    replace: str | None = typer.Option(None, "--replace"),
    word: str | None = typer.Option(None, "--word"),
    item: list[str] = typer.Option([], "--item"),
    where_tag: str | None = typer.Option(None, "--where-tag"),
    where_included: str | None = typer.Option(None, "--where-included"),
    where_caption_format: str | None = typer.Option(None, "--where-caption-format"),
    all_items: bool = typer.Option(False, "--all"),
) -> None:
    _caption_change_command(
        ctx,
        draft,
        operation,
        apply=True,
        find=find,
        replace=replace,
        word=word,
        item=item,
        where_tag=where_tag,
        where_included=where_included,
        where_caption_format=where_caption_format,
        all_items=all_items,
    )


captions_app.command("preview")(_caption_preview_options)
captions_app.command("apply")(_caption_apply_options)


caption_app.command("preview")(_caption_preview_options)
caption_app.command("apply")(_caption_apply_options)





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
        raise typer.BadParameter(
            "An explicit scope is required: --item, --tag, --included, --caption-format, or --all"
        )
    parameters = {
        key: value
        for key, value in {"find": find, "replace": replace, "word": word}.items()
        if value is not None
    }
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
    caption_operation(
        ctx,
        draft,
        "replace",
        preview=preview,
        find=find,
        replace=replace,
        item=item,
        tag=tag,
        included=included,
        caption_format=caption_format,
        all_items=all_items,
    )


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
    caption_operation(
        ctx,
        draft,
        "add_word",
        preview=preview,
        word=word,
        item=item,
        tag=tag,
        included=included,
        caption_format=caption_format,
        all_items=all_items,
    )


@caption_app.command("remove-word")
def caption_remove_word(
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
    caption_operation(
        ctx,
        draft,
        "remove_word",
        preview=preview,
        word=word,
        item=item,
        tag=tag,
        included=included,
        caption_format=caption_format,
        all_items=all_items,
    )


@caption_app.command("set-included")
def caption_set_included(
    ctx: typer.Context,
    draft: str,
    included_value: bool = typer.Option(..., "--included/--excluded"),
    preview: bool = typer.Option(False),
    item: list[str] = typer.Option([], "--item"),
    tag: str | None = None,
    caption_format: str | None = None,
    all_items: bool = typer.Option(False, "--all"),
) -> None:
    caption_operation(
        ctx,
        draft,
        "set_included",
        preview=preview,
        item=item,
        tag=tag,
        caption_format=caption_format,
        all_items=all_items,
        included=included_value,
    )


@caption_app.command("format")
def caption_format(
    ctx: typer.Context, draft: str, value: str = typer.Option(...)
) -> None:
    if value not in {"text", "json"}:
        raise typer.BadParameter("--value must be text or json")
    call(
        ctx,
        lambda client: client.patch(
            f"/api/dataset-drafts/{draft}/caption-format", {"caption_format": value}
        ),
    )


@caption_app.command("undo")
def caption_undo(ctx: typer.Context, draft: str, operation: str) -> None:
    call(
        ctx,
        lambda client: client.post(
            f"/api/dataset-drafts/{draft}/operations/{operation}/undo"
        ),
    )


@caption_app.command("history")
def caption_history(ctx: typer.Context, draft: str) -> None:
    call(ctx, lambda client: client.get(f"/api/dataset-drafts/{draft}/operations"))


@caption_app.command("set")
def caption_set(
    ctx: typer.Context, draft: str, item: str, text: str = typer.Option(...)
) -> None:
    call(
        ctx,
        lambda client: client.patch(
            f"/api/dataset-drafts/{draft}/items/{item}", {"caption": text}
        ),
    )


@caption_app.command("generate")
def caption_generate(
    ctx: typer.Context,
    draft: str,
    prompt: str = typer.Option(..., "--prompt"),
    model: str | None = typer.Option(None, "--model"),
    caption_format: str = typer.Option("text", "--caption-format"),
    item: list[str] = typer.Option([], "--item"),
    all_items: bool = typer.Option(False, "--all"),
) -> None:
    if bool(item) == all_items:
        raise typer.BadParameter("choose either --item or --all")
    body = {
        "prompt": prompt,
        "model": model,
        "caption_format": caption_format,
        "item_ids": item or None,
        "all": all_items,
    }
    call(ctx, lambda client: client.post(f"/api/dataset-drafts/{draft}/caption", body))


run_app = typer.Typer(no_args_is_help=True)
app.add_typer(run_app, name="run")


@run_app.command("list")
def run_list(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project"),
    status: str | None = typer.Option(None, "--status"),
) -> None:
    call(ctx, lambda client: client.get("/api/runs", project_id=project, status=status))


@run_app.command("show")
def run_show(ctx: typer.Context, run: str) -> None:
    call(ctx, lambda client: client.get(f"/api/runs/{run}"))


@run_app.command("live")
def run_live(ctx: typer.Context, run: str) -> None:
    """Return the current status, summaries, upload counts, and sample count."""
    call(ctx, lambda client: client.get(f"/api/runs/{run}/live"))


@run_app.command("metrics")
def run_metrics(
    ctx: typer.Context,
    run: str,
    name: str = typer.Option("loss", "--name"),
) -> None:
    """Return ordered points for one scalar metric."""
    call(ctx, lambda client: client.get(f"/api/runs/{run}/metrics", name=name))


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
def run_samples(
    ctx: typer.Context,
    run: str,
    step: int | None = typer.Option(None, "--step"),
    limit: int = typer.Option(100, "--limit"),
) -> None:
    call(
        ctx,
        lambda client: client.get(f"/api/runs/{run}/samples", step=step, limit=limit),
    )


@run_app.command("lineage")
def run_lineage(ctx: typer.Context, run: str) -> None:
    call(
        ctx,
        lambda client: client.get("/api/lineage", subject_type="run", subject_id=run),
    )


checkpoint_app = typer.Typer(no_args_is_help=True)
app.add_typer(checkpoint_app, name="checkpoint")


@checkpoint_app.command("show")
def checkpoint_show(ctx: typer.Context, checkpoint: str) -> None:
    call(ctx, lambda client: client.get(f"/api/checkpoints/{checkpoint}"))


@checkpoint_app.command("download")
def checkpoint_download(
    ctx: typer.Context, checkpoint: str, output: Path | None = None, wait: bool = True
) -> None:
    result = call(
        ctx,
        lambda client: client.post(
            f"/api/checkpoints/{checkpoint}/hydrate",
            {"output": str(output) if output else None},
        ),
        emit_result=not wait,
    )
    if wait and isinstance(result, dict) and result.get("job_id"):
        wait_for_job(ctx, result["job_id"])
    elif wait:
        emit(result, json_output=rt(ctx).json_output, jsonl=rt(ctx).jsonl)


@checkpoint_app.command("verify")
def checkpoint_verify(ctx: typer.Context, checkpoint: str) -> None:
    """Verify the checkpoint's persisted handoff and storage evidence."""

    def verify(client: TitlesClient) -> dict[str, Any]:
        row = client.get(f"/api/checkpoints/{checkpoint}")
        artifact = row.get("artifact") or {}
        digest = str(artifact.get("sha256") or "")
        storage = (
            artifact.get("storage") if isinstance(artifact.get("storage"), list) else []
        )
        evidenced = [
            location
            for location in storage
            if str(location.get("verification_state") or "").lower()
            in {"available", "verified"}
            and location.get("size") is not None
        ]
        checks = {
            "artifact_registered": bool(artifact.get("asset_id")),
            "sha256_recorded": len(digest) == 64
            and all(character in "0123456789abcdefABCDEF" for character in digest),
            "storage_evidence_recorded": bool(evidenced),
            "checkpoint_revision_recorded": bool(
                artifact.get("checkpoint_revision_id")
            ),
        }
        return {
            "checkpoint_id": checkpoint,
            "verified": all(checks.values()),
            "scope": "persisted_handoff_evidence",
            "checks": checks,
            "sha256": digest or None,
            "locations": evidenced,
        }

    call(ctx, verify)


model_app = typer.Typer(no_args_is_help=True)
app.add_typer(model_app, name="model")


@model_app.command("list")
def model_list(ctx: typer.Context, project: str | None = None) -> None:
    call(ctx, lambda client: client.get("/api/models", project_id=project))


@model_app.command("create")
def model_create(
    ctx: typer.Context, project: str = typer.Option(...), name: str = typer.Option(...)
) -> None:
    call(
        ctx,
        lambda client: client.post(
            "/api/models", {"project_id": project, "name": name}
        ),
    )


@model_app.command("version-create")
def model_version_create(
    ctx: typer.Context,
    model: str,
    checkpoint: str = typer.Option(...),
    state: str = typer.Option("candidate"),
    name: str | None = typer.Option(None),
) -> None:
    call(
        ctx,
        lambda client: client.post(
            "/api/model-versions",
            {
                "model_id": model,
                "checkpoint_id": checkpoint,
                "lifecycle_state": state,
                "name": name or f"checkpoint-{checkpoint[:8]}",
            },
        ),
    )


@model_app.command("version-show")
def model_version_show(ctx: typer.Context, version: str) -> None:
    call(ctx, lambda client: client.get(f"/api/model-versions/{version}"))


@model_app.command("version-approve")
def model_version_approve(ctx: typer.Context, version: str) -> None:
    call(
        ctx,
        lambda client: client.patch(
            f"/api/model-versions/{version}/state", {"lifecycle_state": "approved"}
        ),
    )


@model_app.command("version-archive")
def model_version_archive(ctx: typer.Context, version: str) -> None:
    call(
        ctx,
        lambda client: client.patch(
            f"/api/model-versions/{version}/state", {"lifecycle_state": "archived"}
        ),
    )


prompt_app = typer.Typer(no_args_is_help=True)
app.add_typer(prompt_app, name="prompt-set")


@prompt_app.command("list")
def prompt_list(ctx: typer.Context, project: str | None = None) -> None:
    call(ctx, lambda client: client.get("/api/prompt-sets", project_id=project))


@prompt_app.command("show")
def prompt_show(ctx: typer.Context, prompt_set: str) -> None:
    call(ctx, lambda client: client.get(f"/api/prompt-sets/{prompt_set}"))


from .plan import (
    CONTRACT_VERSION,
    digest,
    load_prompts,
    normalize_cases,
    parse_axis,
    parse_json_object,
    request_plan,
    validate_cardinality,
)

GRID_PREFLIGHT_ROUTE = "/api/projects/{project_id}/experiment-plans/preflight"


def parse_target(value: str, ordinal: int) -> dict[str, Any]:
    try:
        target = json.loads(value)
    except json.JSONDecodeError:
        parts = value.split(":")
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            raise typer.BadParameter(
                "target must be JSON or provider:endpoint[:model_version[:checkpoint]]"
            )
        target = {"provider": parts[0], "endpoint_id": parts[1]}
        if len(parts) > 2 and parts[2]:
            target["model_version_id"] = parts[2]
        if len(parts) > 3 and parts[3]:
            target["checkpoint_revision_id"] = parts[3]
    if not isinstance(target, dict):
        raise typer.BadParameter("target must be a JSON object")
    if (
        not str(target.get("provider") or "").strip()
        or not str(target.get("endpoint_id") or "").strip()
    ):
        raise typer.BadParameter("target requires provider and endpoint_id")
    target = {
        key: target[key]
        for key in (
            "target_id",
            "ordinal",
            "provider",
            "endpoint_id",
            "model_version_id",
            "checkpoint_revision_id",
            "overrides",
        )
        if key in target
    }
    target.setdefault("target_id", f"target-{ordinal}")
    target.setdefault("ordinal", ordinal)
    target.setdefault("overrides", {})
    return target


def build_plan(
    *,
    project: str,
    prompt: list[str],
    prompt_file: Path | None,
    target: list[str],
    x_axis: str,
    x_value: list[str],
    y_axis: str,
    y_value: list[str],
    z_axis: str | None = None,
    z_value: list[str] | None = None,
    shared: list[str] | None = None,
    target_override: list[str] | None = None,
) -> dict[str, Any]:
    cases = normalize_cases(prompt) if prompt else []
    if prompt_file is not None:
        cases.extend(load_prompts(prompt_file))
    if not cases:
        raise typer.BadParameter("at least one --prompt or --prompt-file is required")
    cases = [
        {
            **case,
            "ordinal": index,
            "case_id": f"case-{index}",
            "input_digest": digest(case["input"]),
        }
        for index, case in enumerate(cases)
    ]
    targets = [parse_target(value, index) for index, value in enumerate(target)]
    if not targets:
        raise typer.BadParameter("at least one --target is required")
    overrides = [
        parse_json_object(value, "--target-override")
        for value in (target_override or [])
    ]
    if overrides and len(overrides) not in {1, len(targets)}:
        raise typer.BadParameter(
            "--target-override requires one object or one object per target"
        )
    for index, item in enumerate(overrides):
        targets[index if len(overrides) > 1 else 0]["overrides"] = item
    plan = {
        "contract_version": CONTRACT_VERSION,
        "axes": {
            "x": parse_axis(x_axis, x_value),
            "y": parse_axis(y_axis, y_value),
            "z": parse_axis(z_axis, z_value or []) if z_axis else None,
        },
        "cases": cases,
        "targets": targets,
        "shared_params": parse_params(shared or []),
    }
    try:
        validate_cardinality(plan)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return plan


FAL_BILLING_ACKNOWLEDGEMENT = (
    "I understand FAL may bill this run even if checkpoint fetch fails"
)


def fal_image_projection(
    client: TitlesClient,
    *,
    model_version: str,
    prompt: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    version = client.get(f"/api/model-versions/{model_version}")
    project_id = str(version.get("project_id") or "")
    model_id = str(version.get("model_id") or "")
    checkpoint_revision_id = str(version.get("checkpoint_revision_id") or "")
    if not project_id or not checkpoint_revision_id:
        raise ApiError(
            "Model version has no immutable checkpoint revision",
            status_code=409,
            code="CHECKPOINT_REVISION_REQUIRED",
        )
    return client.post(
        "/api/generation-requests/compile",
        {
            "workflow": "image",
            "provider": "fal",
            "context": {
                "kind": "model",
                "project_id": project_id,
                "model_id": model_id or None,
            },
            "model_version_id": model_version,
            "checkpoint_revision_id": checkpoint_revision_id,
            "prompt": prompt,
            "parameters": parameters,
            "client_request_id": str(uuid.uuid4()),
        },
    )


image_app = typer.Typer(no_args_is_help=True)
app.add_typer(image_app, name="image")


@image_app.command("generate")
def image_generate(
    ctx: typer.Context,
    model_version: str = typer.Option(...),
    prompt: str = typer.Option(...),
    param: list[str] = typer.Option([], "--param"),
    dry_run: bool = False,
    wait: bool = False,
) -> None:
    def operation(client: TitlesClient):
        projection = fal_image_projection(
            client,
            model_version=model_version,
            prompt=prompt,
            parameters=parse_params(param),
        )
        if dry_run:
            return projection
        admission = projection.get("admission") or {}
        if admission.get("state") != "ready":
            return projection
        accepted = client.post(
            f"/api/fal-admissions/{admission['id']}/admit",
            {
                "expected_version": admission["version"],
                "billing_acknowledgement": FAL_BILLING_ACKNOWLEDGEMENT,
            },
        )
        if accepted.get("eval_run_id"):
            run = client.get(f"/api/eval-runs/{accepted['eval_run_id']}")
            accepted["job_id"] = run.get("job_id")
        return accepted

    result = call(ctx, operation, emit_result=not wait)
    if wait and isinstance(result, dict) and result.get("job_id"):
        wait_for_job(ctx, result["job_id"])
    elif wait:
        emit(result, json_output=rt(ctx).json_output, jsonl=rt(ctx).jsonl)


eval_app = typer.Typer(no_args_is_help=True)
app.add_typer(eval_app, name="eval")


@eval_app.command("endpoints")
def eval_endpoints(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/eval-endpoints"))


@eval_app.command("schema")
def eval_schema(ctx: typer.Context, endpoint: str) -> None:
    call(ctx, lambda client: client.get(f"/api/eval-endpoints/{endpoint}/schema"))


@eval_app.command("create")
def eval_create(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project"),
    name: str = typer.Option(..., "--name"),
    run_id: str = typer.Option(..., "--run-id"),
    assessment_kind: str = typer.Option(..., "--assessment-kind"),
    assessment_param: list[str] = typer.Option([], "--assessment-param"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
) -> None:

    body = {
        "name": name,
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "run_id": run_id,
        "assessment": {
            "kind": assessment_kind,
            "parameters": parse_params(assessment_param),
        },
    }
    call(ctx, lambda client: client.post(f"/api/projects/{project}/evals", body))


@eval_app.command("list")
def eval_list(
    ctx: typer.Context, project: str = typer.Option(..., "--project")
) -> None:
    call(ctx, lambda client: client.get(f"/api/projects/{project}/evals"))


@eval_app.command("show")
def eval_show(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project"),
    eval_id: str = typer.Argument(...),
) -> None:
    call(ctx, lambda client: client.get(f"/api/projects/{project}/evals/{eval_id}"))


@eval_app.command("lineage")
def eval_lineage(ctx: typer.Context, eval_id: str = typer.Argument(...)) -> None:
    call(
        ctx,
        lambda client: client.get(
            "/api/lineage", subject_type="eval_run", subject_id=eval_id
        ),
    )


grid_app = typer.Typer(no_args_is_help=True)
app.add_typer(grid_app, name="grid")


def grid_plan_from_options(
    project: str,
    prompt: list[str],
    prompt_file: Path | None,
    target: list[str],
    x_axis: str,
    x_value: list[str],
    y_axis: str,
    y_value: list[str],
    z_axis: str | None,
    z_value: list[str],
    shared: list[str],
    target_override: list[str],
) -> dict[str, Any]:
    return build_plan(
        project=project,
        prompt=prompt,
        prompt_file=prompt_file,
        target=target,
        x_axis=x_axis,
        x_value=x_value,
        y_axis=y_axis,
        y_value=y_value,
        z_axis=z_axis,
        z_value=z_value,
        shared=shared,
        target_override=target_override,
    )


@grid_app.command("preflight")
def grid_preflight(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project"),
    prompt: list[str] = typer.Option([], "--prompt"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    target: list[str] = typer.Option([], "--target"),
    x_axis: str = typer.Option(..., "--x-axis"),
    x_value: list[str] = typer.Option([], "--x-value"),
    y_axis: str = typer.Option(..., "--y-axis"),
    y_value: list[str] = typer.Option([], "--y-value"),
    z_axis: str | None = typer.Option(None, "--z-axis"),
    z_value: list[str] = typer.Option([], "--z-value"),
    shared: list[str] = typer.Option([], "--shared"),
    target_override: list[str] = typer.Option([], "--target-override"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
) -> None:
    plan = grid_plan_from_options(
        project,
        prompt,
        prompt_file,
        target,
        x_axis,
        x_value,
        y_axis,
        y_value,
        z_axis,
        z_value,
        shared,
        target_override,
    )
    body = {
        **request_plan(plan),
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }
    call(
        ctx,
        lambda client: client.post(
            GRID_PREFLIGHT_ROUTE.format(project_id=project), body
        ),
    )


@grid_app.command("create")
def grid_create(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project"),
    name: str = typer.Option(..., "--name"),
    prompt: list[str] = typer.Option([], "--prompt"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    target: list[str] = typer.Option([], "--target"),
    x_axis: str = typer.Option(..., "--x-axis"),
    x_value: list[str] = typer.Option([], "--x-value"),
    y_axis: str = typer.Option(..., "--y-axis"),
    y_value: list[str] = typer.Option([], "--y-value"),
    z_axis: str | None = typer.Option(None, "--z-axis"),
    z_value: list[str] = typer.Option([], "--z-value"),
    shared: list[str] = typer.Option([], "--shared"),
    target_override: list[str] = typer.Option([], "--target-override"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
) -> None:
    plan = grid_plan_from_options(
        project,
        prompt,
        prompt_file,
        target,
        x_axis,
        x_value,
        y_axis,
        y_value,
        z_axis,
        z_value,
        shared,
        target_override,
    )
    call(
        ctx,
        lambda client: client.post(
            f"/api/projects/{project}/grids",
            {
                "name": name,
                "idempotency_key": idempotency_key or str(uuid.uuid4()),
                "plan": request_plan(plan),
            },
        ),
    )


@grid_app.command("list")
def grid_list(
    ctx: typer.Context, project: str = typer.Option(..., "--project")
) -> None:
    call(ctx, lambda client: client.get(f"/api/projects/{project}/grids"))


@grid_app.command("show")
def grid_show(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project"),
    grid_id: str = typer.Argument(...),
) -> None:
    call(ctx, lambda client: client.get(f"/api/projects/{project}/grids/{grid_id}"))


@grid_app.command("queue")
def grid_queue(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project"),
    grid_id: str = typer.Argument(...),
    plan_version: int = typer.Option(..., "--plan-version"),
    plan_digest: str = typer.Option(..., "--plan-digest"),
    cell_ordinal: list[int] = typer.Option([], "--cell-ordinal"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    acknowledge_billing: bool = typer.Option(False, "--acknowledge-billing"),
) -> None:
    if not acknowledge_billing:
        raise typer.BadParameter(
            "--acknowledge-billing is required before queueing provider work"
        )
    body = {
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "plan_version": plan_version,
        "plan_digest": plan_digest,
        "cell_ordinals": cell_ordinal,
        "billing_acknowledgement": FAL_BILLING_ACKNOWLEDGEMENT,
    }
    call(
        ctx,
        lambda client: client.post(
            f"/api/projects/{project}/grids/{grid_id}/queue", body
        ),
    )


gallery_app = typer.Typer(no_args_is_help=True)
app.add_typer(gallery_app, name="gallery")


@gallery_app.command("list")
def gallery_list(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project"),
    kind: str | None = typer.Option(None, "--kind"),
    query: str | None = typer.Option(None, "--query", "-q"),
    decision: str | None = typer.Option(None, "--decision"),
    rating: int | None = typer.Option(None, "--rating", min=1, max=5),
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    call(
        ctx,
        lambda client: client.get(
            "/api/gallery",
            project_id=project,
            kind=kind,
            q=query,
            decision=decision,
            rating=rating,
            limit=limit,
            offset=offset,
        ),
    )


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


@asset_app.command("repair-images")
def asset_repair_images(
    ctx: typer.Context,
    project: str = typer.Option(..., "--project"),
    wait: bool = typer.Option(True, "--wait/--no-wait"),
) -> None:
    """Repair legacy cache-backed image records from their recorded source."""
    result = call(
        ctx,
        lambda client: client.post(
            "/api/jobs",
            {
                "kind": "image.repair_lineage",
                "payload": {"project_id": project},
            },
        ),
        emit_result=not wait,
    )
    if wait and isinstance(result, dict) and result.get("id"):
        wait_for_job(ctx, result["id"])
    elif wait:
        emit(result, json_output=rt(ctx).json_output, jsonl=rt(ctx).jsonl)


@asset_app.command("lineage")
def asset_lineage(ctx: typer.Context, asset: str) -> None:
    call(
        ctx,
        lambda client: client.get(
            "/api/lineage", subject_type="asset", subject_id=asset
        ),
    )


@asset_app.command("delete")
def asset_delete(
    ctx: typer.Context,
    asset: str,
    delete_content: bool = typer.Option(
        False, help="Also delete supported local content"
    ),
) -> None:
    content = "true" if delete_content else "false"
    call(
        ctx,
        lambda client: client.delete(
            f"/api/assets/{asset}?report=true&delete_content={content}"
        ),
    )


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
    call(
        ctx,
        lambda client: client.post(
            "/api/reviews",
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "rating": rating,
                "decision": decision,
            },
        ),
    )


@review_app.command("show")
def review_show(ctx: typer.Context, subject: str) -> None:
    subject_type, subject_id = split_subject(subject)
    call(
        ctx,
        lambda client: client.get(
            "/api/reviews", subject_type=subject_type, subject_id=subject_id
        ),
    )


@review_app.command("list")
def review_list(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project"),
    decision: str | None = typer.Option(None, "--decision"),
    profile: str | None = typer.Option(None, "--profile"),
) -> None:
    call(
        ctx,
        lambda client: client.get(
            "/api/reviews", project_id=project, decision=decision, profile_id=profile
        ),
    )


comment_app = typer.Typer(no_args_is_help=True)
app.add_typer(comment_app, name="comment")


@comment_app.command("list")
def comment_list(ctx: typer.Context, subject: str) -> None:
    subject_type, subject_id = split_subject(subject)
    call(
        ctx,
        lambda client: client.get(
            "/api/comments", subject_type=subject_type, subject_id=subject_id
        ),
    )


@comment_app.command("add")
def comment_add(
    ctx: typer.Context, subject: str, text: str = typer.Option(...)
) -> None:
    subject_type, subject_id = split_subject(subject)
    call(
        ctx,
        lambda client: client.post(
            "/api/comments",
            {"subject_type": subject_type, "subject_id": subject_id, "body": text},
        ),
    )


note_app = typer.Typer(no_args_is_help=True)
app.add_typer(note_app, name="note")


@note_app.command("list")
def note_list(ctx: typer.Context, subject: str) -> None:
    subject_type, subject_id = split_subject(subject)
    call(
        ctx,
        lambda client: client.get(
            "/api/notes", subject_type=subject_type, subject_id=subject_id
        ),
    )


@note_app.command("add")
def note_add(ctx: typer.Context, subject: str, file: Path = typer.Option(...)) -> None:
    subject_type, subject_id = split_subject(subject)
    call(
        ctx,
        lambda client: client.post(
            "/api/notes",
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "body": file.read_text(encoding="utf-8"),
            },
        ),
    )


activity_app = typer.Typer(no_args_is_help=True)
app.add_typer(activity_app, name="activity")


@activity_app.command("list")
def activity_list(
    ctx: typer.Context,
    project: str | None = typer.Option(None, "--project"),
    profile: str | None = typer.Option(None, "--profile"),
    since: str | None = typer.Option(None, "--since"),
    limit: int = typer.Option(100, "--limit"),
) -> None:
    call(
        ctx,
        lambda client: client.get(
            "/api/activity",
            project_id=project,
            profile_id=profile,
            since=since,
            limit=limit,
        ),
    )


lineage_app = typer.Typer(no_args_is_help=True)
app.add_typer(lineage_app, name="lineage")


@lineage_app.command("show")
def lineage_show(ctx: typer.Context, subject: str) -> None:
    subject_type, subject_id = split_subject(subject)
    call(
        ctx,
        lambda client: client.get(
            "/api/lineage", subject_type=subject_type, subject_id=subject_id
        ),
    )


@lineage_app.command("path")
def lineage_path(
    ctx: typer.Context,
    source: str,
    target: str,
    max_depth: int = typer.Option(5, min=1, max=20),
) -> None:
    """Find one directed lineage path between typed subjects."""
    source_node = split_subject(source)
    target_node = split_subject(target)

    def find_path(client: TitlesClient) -> dict[str, Any]:
        frontier: list[tuple[tuple[str, str], list[dict[str, Any]]]] = [
            (source_node, [])
        ]
        visited = {source_node}
        for _depth in range(max_depth):
            next_frontier: list[tuple[tuple[str, str], list[dict[str, Any]]]] = []
            for node, path in frontier:
                edges = client.get(
                    "/api/lineage", subject_type=node[0], subject_id=node[1]
                )
                for edge in edges if isinstance(edges, list) else []:
                    if (
                        str(edge.get("source_type")),
                        str(edge.get("source_id")),
                    ) != node:
                        continue
                    next_node = (
                        str(edge.get("target_type")),
                        str(edge.get("target_id")),
                    )
                    next_path = [*path, edge]
                    if next_node == target_node:
                        return {
                            "source": source,
                            "target": target,
                            "depth": len(next_path),
                            "edges": next_path,
                        }
                    if next_node not in visited:
                        visited.add(next_node)
                        next_frontier.append((next_node, next_path))
            frontier = next_frontier
            if not frontier:
                break
        raise ApiError(
            f"No directed lineage path found from {source} to {target}",
            status_code=4,
            code="LINEAGE_PATH_NOT_FOUND",
            details={"source": source, "target": target, "max_depth": max_depth},
        )

    call(ctx, find_path)


connection_app = typer.Typer(no_args_is_help=True)
app.add_typer(connection_app, name="connection")


@connection_app.command("list")
def connection_list(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/connections"))


@connection_app.command("test")
def connection_test(ctx: typer.Context, provider: str) -> None:
    call(ctx, lambda client: client.post(f"/api/connections/{provider}/test"))


settings_app = typer.Typer(no_args_is_help=True)
app.add_typer(settings_app, name="settings")


@settings_app.command("show")
def settings_show(ctx: typer.Context) -> None:
    call(ctx, lambda client: client.get("/api/operator-settings"))


@settings_app.command("update")
def settings_update(
    ctx: typer.Context,
    file: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
) -> None:
    payload = read_json(file)
    if not isinstance(payload, dict):
        raise typer.BadParameter("--file must contain a JSON object")
    call(ctx, lambda client: client.patch("/api/operator-settings", payload))


@settings_app.command("test")
def settings_test(ctx: typer.Context, provider: str) -> None:
    if provider not in {"llm", "fal", "storage"}:
        raise typer.BadParameter("provider must be llm, fal, or storage")
    call(ctx, lambda client: client.post(f"/api/operator-settings/test/{provider}", {}))


backup_app = typer.Typer(no_args_is_help=True)
app.add_typer(backup_app, name="backup")


def backup_paths(
    data_root: Path | None, backup_root: Path | None
) -> tuple[Path, Path, str]:
    if data_root is not None:
        root = data_root.expanduser().resolve()
        database_name = "modelfiche.sqlite3"
    else:
        database_url = os.getenv("TITLES_DATABASE_URL")
        if database_url and database_url.startswith("sqlite"):
            from sqlalchemy.engine import make_url

            database_path = (
                Path(make_url(database_url).database or "").expanduser().resolve()
            )
            root, database_name = database_path.parent, database_path.name
        else:
            root = (project_root() / "var").resolve()
            database_name = "titles-dam.sqlite3"
    archives = (
        backup_root.expanduser().resolve()
        if backup_root
        else root.parent / f"{root.name}-backups"
    )
    return root, archives, database_name


@backup_app.command("create")
def backup_create(
    ctx: typer.Context,
    data_root: Path | None = typer.Option(None, "--data-root"),
    backup_root: Path | None = typer.Option(None, "--backup-root"),
    database_only: bool = typer.Option(
        False,
        "--database-only",
        help="Back up only the consistent SQLite database; remote asset locations remain external.",
    ),
) -> None:
    from titles_api.storage.installation_backup import InstallationBackupStore

    root, archives, database_name = backup_paths(data_root, backup_root)
    receipt = InstallationBackupStore(archives).create(
        root,
        database_name,
        database_only=database_only,
    )
    emit(
        {
            "backup_id": receipt.backup_id,
            "archive": str(receipt.archive),
            "created_at": receipt.created_at,
            "files": receipt.files,
            "bytes": receipt.bytes,
            "sha256": receipt.sha256,
            "scope": "database_only" if database_only else "full",
        },
        json_output=rt(ctx).json_output,
    )


@backup_app.command("list")
def backup_list(
    ctx: typer.Context,
    data_root: Path | None = typer.Option(None, "--data-root"),
    backup_root: Path | None = typer.Option(None, "--backup-root"),
) -> None:
    _root, archives, _database_name = backup_paths(data_root, backup_root)
    rows = (
        [
            {"archive": str(path), "bytes": path.stat().st_size}
            for path in sorted(archives.glob("modelfiche-*.tar.gz"), reverse=True)
        ]
        if archives.exists()
        else []
    )
    emit(rows, json_output=rt(ctx).json_output, jsonl=rt(ctx).jsonl)


@backup_app.command("verify")
def backup_verify(
    ctx: typer.Context,
    archive: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    backup_root: Path | None = typer.Option(None, "--backup-root"),
) -> None:
    from titles_api.storage.installation_backup import InstallationBackupStore

    archives = (
        backup_root.expanduser().resolve()
        if backup_root
        else archive.expanduser().resolve().parent
    )
    digest = InstallationBackupStore(archives).verify(archive)
    emit(
        {"ok": True, "archive": str(archive.expanduser().resolve()), "sha256": digest},
        json_output=rt(ctx).json_output,
    )


@backup_app.command("restore")
def backup_restore(
    ctx: typer.Context,
    archive: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    data_root: Path | None = typer.Option(None, "--data-root"),
    backup_root: Path | None = typer.Option(None, "--backup-root"),
    database_only: bool = typer.Option(
        False,
        "--database-only",
        help="Restore a database-only archive without replacing the data directory.",
    ),
    confirm: str = typer.Option(..., "--confirm"),
) -> None:
    from titles_api.storage.installation_backup import InstallationBackupStore

    if confirm != "RESTORE MODELFICHE BACKUP":
        raise typer.BadParameter(
            '--confirm must exactly equal "RESTORE MODELFICHE BACKUP"'
        )
    runtime = rt(ctx)
    if _url_is_ready(f"{runtime.context.api_url.rstrip('/')}/api/health"):
        raise typer.BadParameter("stop Modelfiche before restoring a backup")
    root, archives, database_name = backup_paths(data_root, backup_root)
    store = InstallationBackupStore(archives)
    if database_only:
        destination = root / database_name
        store.restore_database(archive, destination)
        restored = destination
    else:
        store.recover(root)
        store.restore(archive, root)
        restored = root
    emit(
        {
            "ok": True,
            "archive": str(archive.expanduser().resolve()),
            "restored": str(restored),
            "scope": "database_only" if database_only else "full",
        },
        json_output=runtime.json_output,
    )


@backup_app.command("recover")
def backup_recover(
    ctx: typer.Context, data_root: Path | None = typer.Option(None, "--data-root")
) -> None:
    from titles_api.storage.installation_backup import InstallationBackupStore

    root, _archives, _database_name = backup_paths(data_root, None)
    recovered = InstallationBackupStore.recover(root)
    emit(
        {"ok": True, "recovered": recovered, "data_root": str(root)},
        json_output=rt(ctx).json_output,
    )


storage_app = typer.Typer(no_args_is_help=True)


def _single_copy_context(
    ctx: typer.Context,
    database: Path | None,
    source_reference: str | None = None,
):
    from sqlalchemy import or_, select
    from sqlalchemy.orm import sessionmaker

    from titles_api import models
    from titles_api.database import build_engine

    engine = build_engine(f"sqlite:///{storage_database_path(database)}")
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    reference = rt(ctx).workspace
    if reference:
        workspace = session.scalar(
            select(models.Workspace).where(
                or_(
                    models.Workspace.id == reference,
                    models.Workspace.slug == reference,
                )
            )
        )
    else:
        workspaces = list(
            session.scalars(
                select(models.Workspace).order_by(models.Workspace.created_at)
            )
        )
        if len(workspaces) != 1:
            session.close()
            engine.dispose()
            raise typer.BadParameter(
                "pass --workspace when the database contains multiple workspaces"
            )
        workspace = workspaces[0]
    if workspace is None:
        session.close()
        engine.dispose()
        raise typer.BadParameter("workspace was not found")
    source = None
    if source_reference is not None:
        sources = list(
            session.scalars(
                select(models.ImportSource).where(
                    models.ImportSource.workspace_id == workspace.id,
                    models.ImportSource.is_active.is_(True),
                )
            )
        )
        matches = [
            item
            for item in sources
            if source_reference == item.id
            or source_reference.casefold() == item.name.casefold()
        ]
        if len(matches) != 1:
            session.close()
            engine.dispose()
            raise typer.BadParameter(
                "--source must identify exactly one active source in the workspace"
            )
        source = matches[0]
    return engine, session, workspace, source


def _single_copy_roots() -> tuple[Path, Path]:
    from titles_api.settings import get_settings

    settings = get_settings()
    return settings.asset_root, settings.cache_root


single_copy_app = typer.Typer(no_args_is_help=True)
storage_app.add_typer(single_copy_app, name="single-copy")


@single_copy_app.command("audit")
def storage_single_copy_audit(
    ctx: typer.Context,
    source: str = typer.Option(..., "--source"),
    database: Path | None = typer.Option(None, "--database"),
) -> None:
    from dataclasses import asdict

    from titles_api.storage.single_copy import audit_single_copy

    engine, session, workspace, selected = _single_copy_context(
        ctx, database, source
    )
    try:
        report = audit_single_copy(
            session,
            workspace.id,
            selected.id,
            _single_copy_roots(),
        )
        emit(asdict(report), json_output=rt(ctx).json_output)
    finally:
        session.close()
        engine.dispose()


@single_copy_app.command("ensure-remote")
def storage_single_copy_ensure_remote(
    ctx: typer.Context,
    source: str = typer.Option(..., "--source"),
    database: Path | None = typer.Option(None, "--database"),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    from dataclasses import asdict

    from titles_api.storage.single_copy import ensure_remote

    if apply and _url_is_ready(f"{rt(ctx).context.api_url.rstrip('/')}/api/health"):
        raise typer.BadParameter("stop Modelfiche before changing storage locations")
    engine, session, workspace, selected = _single_copy_context(
        ctx, database, source
    )
    try:
        report = ensure_remote(
            session,
            workspace.id,
            selected.id,
            _single_copy_roots(),
            dry_run=not apply,
        )
        if apply:
            session.commit()
        else:
            session.rollback()
        emit(asdict(report), json_output=rt(ctx).json_output)
    finally:
        session.close()
        engine.dispose()


@single_copy_app.command("evict-local")
def storage_single_copy_evict_local(
    ctx: typer.Context,
    database: Path | None = typer.Option(None, "--database"),
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    from dataclasses import asdict

    from titles_api.storage.single_copy import evict_local

    if apply and _url_is_ready(f"{rt(ctx).context.api_url.rstrip('/')}/api/health"):
        raise typer.BadParameter("stop Modelfiche before changing storage locations")
    engine, session, workspace, _selected = _single_copy_context(ctx, database)
    try:
        report = evict_local(
            session,
            workspace.id,
            _single_copy_roots(),
            dry_run=not apply,
        )
        if apply:
            session.commit()
        else:
            session.rollback()
        emit(asdict(report), json_output=rt(ctx).json_output)
    finally:
        session.close()
        engine.dispose()
app.add_typer(storage_app, name="storage")


def storage_database_path(value: Path | None) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    return (project_root() / "var" / "titles-dam.sqlite3").resolve()


@storage_app.command("verify-contract")
def storage_verify_contract(
    ctx: typer.Context,
    database: Path | None = typer.Option(None, "--database"),
    run_lease_id: str | None = typer.Option(
        None, "--run-lease-id", envvar="TITLES_RUN_LEASE_ID"
    ),
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
            raise typer.BadParameter(
                "database head/marker is not ready for verification"
            )
        payload = (
            f"{version['version_num']}:{marker['marker']}:{marker['binding_digest']}:1"
        )
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
    from titles_api.storage.cutover import (
        RestoreEligibility,
        restore_raw,
        restore_sanitized,
    )

    if kind not in {"raw", "sanitized"}:
        raise typer.BadParameter("--kind must be raw or sanitized")
    root_path = project_root() / "var" / "cutover"
    store = EnvelopeStore(root_path / "backups", FileKeyStore(root_path / "keys"))
    path = storage_database_path(database)
    if kind == "raw":
        receipt = restore_raw(
            envelope_store=store, backup_id=backup_id, database_path=path
        )
    else:
        with sqlite3.connect(path) as db:
            gates = {
                str(name): str(state)
                for name, state in db.execute(
                    "SELECT name,state FROM storage_feature_gates"
                )
            }
            marker = db.execute(
                "SELECT effect_epoch FROM storage_cutover_marker WHERE id=1"
            ).fetchone()
            if marker is None:
                raise typer.BadParameter("cutover marker is missing")
            effect_rows = 0
            table_names = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in ("publication_receipts", "fal_submission_intents"):
                if table in table_names:
                    effect_rows += int(
                        db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    )
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
        {
            "backup_id": receipt.backup_id,
            "kind": receipt.kind,
            "sha256": receipt.sha256,
            "restored": str(path),
        },
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
        if (candidate / "pyproject.toml").exists() and (
            candidate / "apps" / "cli" / "titles_cli"
        ).is_dir():
            return candidate
    return Path(__file__).resolve().parents[4]

_RUNTIME_ENV_FILE = ".env.1password"
_RUNTIME_ENV_MARKER = "MODELFICHE_RUNTIME_ENV_LOADED"


def _load_runtime_environment(root_path: Path) -> None:
    """Re-exec the start command through 1Password when this checkout defines it."""
    env_file = root_path / _RUNTIME_ENV_FILE
    if (
        not env_file.is_file()
        or os.getenv("TITLES_REMOTE_ACCESS_ORIGIN")
        or os.getenv(_RUNTIME_ENV_MARKER) == "1"
    ):
        return
    op = shutil.which("op")
    if op is None:
        raise typer.BadParameter(
            f"{_RUNTIME_ENV_FILE} requires the 1Password CLI. Install and sign in to `op`, "
            "or set the runtime environment directly."
        )
    env = {
        **os.environ,
        _RUNTIME_ENV_MARKER: "1",
        "TITLES_PROJECT_ROOT": str(root_path),
    }
    os.execvpe(
        op,
        [
            op,
            "run",
            f"--env-file={env_file}",
            "--",
            sys.executable,
            "-m",
            "titles_cli.main",
            *sys.argv[1:],
        ],
        env,
    )


def _pid_is_alive(path: Path) -> tuple[int | None, bool]:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None, False
    from titles_api.platform_io import process_alive
    return pid, process_alive(pid)


def _url_is_ready(url: str, *, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def _terminate_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
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
        "wandb": var / "wandb-ingress.pid",
        "worker": var / "worker.pid",
        "web": var / "web.pid",
    }


def _stack_state(root_path: Path) -> dict[str, Any]:
    var = root_path / "var"
    paths = _service_pid_paths(var)
    api_port = int(os.environ.get("TITLES_API_PORT", "8400"))
    wandb_ingress_port = int(os.environ.get("TITLES_WANDB_INGRESS_PORT", "8401"))
    web_port = int(os.environ.get("TITLES_WEB_PORT", "5173"))
    services: dict[str, dict[str, Any]] = {}
    for name, path in paths.items():
        pid, alive = _pid_is_alive(path)
        healthy = alive
        if name == "api":
            healthy = alive and _url_is_ready(f"http://127.0.0.1:{api_port}/api/health")
        elif name == "wandb":
            healthy = alive and _url_is_ready(
                f"http://127.0.0.1:{wandb_ingress_port}/health"
            )
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
    if os.getenv("MODELFICHE_BUNDLE_ROOT"):
        from .desktop import start_bundle
        state = start_bundle()
        emit(state, json_output=rt(ctx).json_output)
        if not state["ok"]:
            raise typer.Exit(code=8)
        return
    root_path = project_root()
    _load_runtime_environment(root_path)
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
    pythonpath = os.pathsep.join(
        str(root_path / "apps" / name) for name in ("api", "worker", "cli")
    )
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
            detail = (
                (var / "server.error").read_text(encoding="utf-8")
                if (var / "server.error").exists()
                else "service supervisor exited during startup"
            )
            fail(
                ApiError(
                    detail,
                    status_code=8,
                    code="SERVICE_UNAVAILABLE",
                    retryable=True,
                ),
                json_output=rt(ctx).json_output,
            )
        time.sleep(0.1)
    _terminate_pid(supervisor.pid)
    fail(
        ApiError(
            f"service stack did not become ready within {timeout:g}s",
            status_code=8,
            code="SERVICE_UNAVAILABLE",
            retryable=True,
        ),
        json_output=rt(ctx).json_output,
    )


@server_app.command("status")
def server_status(ctx: typer.Context) -> None:
    if os.getenv("MODELFICHE_BUNDLE_ROOT"):
        from .desktop import status_bundle
        state = status_bundle()
        emit(state, json_output=rt(ctx).json_output)
        if not state["ok"]:
            raise typer.Exit(code=8)
        return
    state = _stack_state(project_root())
    emit(state, json_output=rt(ctx).json_output)
    if not state["ok"]:
        raise typer.Exit(code=8)


@server_app.command("stop")
def server_stop(ctx: typer.Context) -> None:
    if os.getenv("MODELFICHE_BUNDLE_ROOT"):
        from .desktop import stop_bundle
        state = stop_bundle()
        emit(state, json_output=rt(ctx).json_output)
        if not state["ok"]:
            raise typer.Exit(code=8)
        return
    var = project_root() / "var"
    pid_paths = _service_pid_paths(var)
    pids: list[int] = []
    for name in ("supervisor", "web", "worker", "wandb", "api"):
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


@app.command("start", help="Start the local API, worker, and frontend.")
def start(ctx: typer.Context) -> None:
    """Convenience alias for `mfiche server start`."""
    server_start(ctx)


@app.command("status", help="Show local API, worker, and frontend status.")
def status(ctx: typer.Context) -> None:
    """Convenience alias for `mfiche server status`."""
    server_status(ctx)


@app.command("stop", help="Stop the local API, worker, and frontend.")
def stop(ctx: typer.Context) -> None:
    """Convenience alias for `mfiche server stop`."""
    server_stop(ctx)


if __name__ == "__main__":
    app()
