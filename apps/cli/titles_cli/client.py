from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 1,
        code: str = "API_ERROR",
        details: Any = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details
        self.retryable = retryable


@dataclass(slots=True)
class LocalContext:
    api_url: str = "http://127.0.0.1:8400"
    profile: str | None = None
    workspace: str | None = None

    @classmethod
    def load(cls) -> "LocalContext":
        path = context_path()
        values: dict[str, Any] = {}
        if path.exists():
            try:
                values = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                values = {}
        return cls(
            api_url=os.getenv("TITLES_API_URL", values.get("api_url", "http://127.0.0.1:8400")),
            profile=os.getenv("TITLES_PROFILE", values.get("profile")),
            workspace=os.getenv("TITLES_WORKSPACE", values.get("workspace")),
        )

    def save(self) -> None:
        path = context_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "api_url": self.api_url,
                    "profile": self.profile,
                    "workspace": self.workspace,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def context_path() -> Path:
    root = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "titles-dam" / "context.json"


class TitlesClient:
    def __init__(
        self,
        *,
        base_url: str,
        profile: str | None = None,
        workspace: str | None = None,
        request_id: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        headers = {"Accept": "application/json"}
        if profile:
            headers["X-Profile-ID"] = profile
        if workspace:
            headers["X-Workspace-ID"] = workspace
        if request_id:
            headers["Idempotency-Key"] = request_id
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )

    def close(self) -> None:
        self.http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        try:
            response = self.http.request(method, path, params=clean(params), json=json_body)
        except httpx.ConnectError as exc:
            raise ApiError(
                f"Titles DAM service is unavailable at {self.http.base_url}",
                status_code=8,
                code="SERVICE_UNAVAILABLE",
                retryable=True,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ApiError(
                "The request timed out. The server-side job may still be running.",
                status_code=6,
                code="REQUEST_TIMEOUT",
                retryable=True,
            ) from exc

        if response.is_success:
            if response.status_code == 204 or not response.content:
                return {"ok": True}
            try:
                return response.json()
            except ValueError:
                return {"ok": True, "text": response.text}

        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text or response.reason_phrase}
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        detail = error.get("detail")
        message = error.get("message") or (
            detail if isinstance(detail, str) else None
        ) or response.reason_phrase
        code = error.get("code") or f"HTTP_{response.status_code}"
        raise ApiError(
            message,
            status_code=http_exit_code(response.status_code),
            code=code,
            details=error.get("details", detail),
            retryable=bool(error.get("retryable", response.status_code >= 500)),
        )

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.request("POST", path, params=params, json_body=body)

    def patch(self, path: str, body: Any = None) -> Any:
        return self.request("PATCH", path, json_body=body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)


def clean(values: dict[str, Any] | None) -> dict[str, Any] | None:
    if values is None:
        return None
    return {key: value for key, value in values.items() if value is not None}


def http_exit_code(status: int) -> int:
    if status == 404:
        return 4
    if status in {409, 412, 422}:
        return 5
    if status in {401, 403}:
        return 3
    if status >= 500:
        return 6
    return 2
