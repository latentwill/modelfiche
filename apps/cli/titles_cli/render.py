from __future__ import annotations

import json
from typing import Any

import typer

from .client import ApiError


def emit(value: Any, *, json_output: bool = False, jsonl: bool = False) -> None:
    if jsonl:
        items = extract_items(value)
        for item in items:
            typer.echo(json.dumps(item, ensure_ascii=True, separators=(",", ":")))
        return
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=True, indent=2, default=str))
        return
    render_human(value)


def extract_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "results", "data"):
            if isinstance(value.get(key), list):
                return value[key]
    return [value]


def render_human(value: Any, *, indent: int = 0) -> None:
    prefix = " " * indent
    if isinstance(value, list):
        for item in value:
            render_human(item, indent=indent)
        return
    if isinstance(value, dict):
        items = extract_items(value)
        if items != [value]:
            for item in items:
                render_human(item, indent=indent)
            cursor = value.get("next_cursor") or value.get("cursor")
            if cursor:
                typer.echo(f"{prefix}next_cursor: {cursor}")
            return
        identity = value.get("name") or value.get("title") or value.get("id")
        if identity:
            typer.echo(f"{prefix}{identity}")
        for key, item in value.items():
            if key in {"name", "title"} and item == identity:
                continue
            if isinstance(item, (dict, list)):
                typer.echo(f"{prefix}{key}:")
                render_human(item, indent=indent + 2)
            elif item is not None:
                typer.echo(f"{prefix}{key}: {item}")
        return
    typer.echo(f"{prefix}{value}")


def fail(error: ApiError, *, json_output: bool = False) -> None:
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
            "details": error.details,
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=True), err=False)
    else:
        typer.echo(f"{error.code}: {error}", err=True)
        if error.details:
            typer.echo(json.dumps(error.details, ensure_ascii=True, indent=2), err=True)
    raise typer.Exit(error.status_code)
