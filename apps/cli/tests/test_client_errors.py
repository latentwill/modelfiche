from __future__ import annotations

import httpx
import pytest

from titles_cli.client import ApiError, TitlesClient


def test_workspace_boundary_error_preserves_actionable_details() -> None:
    client = TitlesClient(base_url="http://localhost")
    client.http.close()
    detail = {
        "code": "workspace_required",
        "message": "X-Workspace-ID is required for API mutations",
    }
    client.http = httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda request: httpx.Response(428, json={"detail": detail})),
    )
    try:
        with pytest.raises(ApiError) as raised:
            client.post("/api/projects", {"name": "Example"})
        error = raised.value
        assert error.code == "workspace_required"
        assert str(error) == detail["message"]
        assert error.status_code == 3
        assert error.details == detail
        assert error.retryable is False
    finally:
        client.close()


def test_nested_precondition_error_retains_recovery_policy() -> None:
    client = TitlesClient(base_url="http://localhost")
    client.http.close()
    detail = {
        "code": "source_temporarily_unavailable",
        "message": "Source verification has not finished",
        "retryable": True,
    }
    client.http = httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda request: httpx.Response(409, json={"detail": detail})),
    )
    try:
        with pytest.raises(ApiError) as raised:
            client.get("/api/assets/example")
        assert raised.value.code == detail["code"]
        assert raised.value.status_code == 5
        assert raised.value.retryable is True
    finally:
        client.close()
