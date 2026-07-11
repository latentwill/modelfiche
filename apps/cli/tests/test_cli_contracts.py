from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from titles_cli import main


runner = CliRunner()


class FakeClient:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, Any]] = []
        self.responses = list(responses or [])

    def _response(self) -> Any:
        return self.responses.pop(0) if self.responses else {"ok": True}

    def get(self, path: str, **params: Any) -> Any:
        self.calls.append(("GET", path, params, None))
        return self._response()

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        self.calls.append(("POST", path, params, body))
        return self._response()

    def close(self) -> None:
        pass


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    fake = FakeClient()
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)
    return fake


@pytest.mark.parametrize(
    ("arguments", "expected_path", "expected_params"),
    [
        (
            ["project", "activity", "project-1", "--limit", "7"],
            "/api/activity",
            {"project_id": "project-1", "limit": 7},
        ),
        (
            ["prompt-set", "list", "--project", "project-1"],
            "/api/prompt-sets",
            {"project_id": "project-1"},
        ),
        (
            [
                "review",
                "list",
                "--project",
                "project-1",
                "--profile",
                "profile-1",
                "--decision",
                "approved",
            ],
            "/api/reviews",
            {
                "project_id": "project-1",
                "profile_id": "profile-1",
                "decision": "approved",
            },
        ),
        (
            [
                "activity",
                "list",
                "--project",
                "project-1",
                "--profile",
                "profile-1",
                "--since",
                "2026-01-01T00:00:00Z",
                "--limit",
                "9",
            ],
            "/api/activity",
            {
                "project_id": "project-1",
                "profile_id": "profile-1",
                "since": "2026-01-01T00:00:00Z",
                "limit": 9,
            },
        ),
    ],
)
def test_project_and_profile_filters_use_openapi_names(
    fake_client: FakeClient,
    arguments: list[str],
    expected_path: str,
    expected_params: dict[str, Any],
) -> None:
    result = runner.invoke(main.app, ["--json", *arguments])

    assert result.exit_code == 0, result.output
    assert fake_client.calls == [("GET", expected_path, expected_params, None)]


@pytest.mark.parametrize(
    ("arguments", "subject_type", "subject_id"),
    [
        (["run", "lineage", "run-1"], "run", "run-1"),
        (["eval", "lineage", "eval-1"], "eval_run", "eval-1"),
        (["asset", "lineage", "asset-1"], "asset", "asset-1"),
        (["lineage", "show", "checkpoint:checkpoint-1"], "checkpoint", "checkpoint-1"),
    ],
)
def test_lineage_commands_use_generic_openapi_route(
    fake_client: FakeClient,
    arguments: list[str],
    subject_type: str,
    subject_id: str,
) -> None:
    result = runner.invoke(main.app, ["--json", *arguments])

    assert result.exit_code == 0, result.output
    assert fake_client.calls == [
        (
            "GET",
            "/api/lineage",
            {"subject_type": subject_type, "subject_id": subject_id},
            None,
        )
    ]


def test_import_refresh_maps_training_run_to_run_refresh(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(main.app, ["--json", "import", "refresh", "run:run-1"])

    assert result.exit_code == 0, result.output
    assert fake_client.calls == [("POST", "/api/runs/run-1/refresh", {}, None)]



def test_import_backfill_metrics_refreshes_one_existing_run(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(main.app, ["--json", "import", "backfill-metrics", "--run", "run-1"])

    assert result.exit_code == 0, result.output
    assert fake_client.calls == [("POST", "/api/runs/run-1/refresh", {}, None)]

@pytest.mark.parametrize(
    "arguments",
    [
        ["checkpoint", "verify", "checkpoint-1"],
        ["comment", "update", "comment-1", "--text", "revised"],
        ["lineage", "path", "asset:a", "checkpoint:b"],
        ["import", "refresh", "dataset:dataset-1"],
    ],
)
def test_unsupported_operations_fail_before_http(
    fake_client: FakeClient,
    arguments: list[str],
) -> None:
    result = runner.invoke(main.app, ["--json", *arguments])

    assert result.exit_code == 5
    assert json.loads(result.output)["error"]["code"] == "UNSUPPORTED_OPERATION"
    assert fake_client.calls == []


def test_s3_stat_finds_object_through_browse(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(
        [
            {
                "objects": [
                    {"key": "runs/step-100.safetensors", "size": 42, "etag": "abc"}
                ],
                "next_cursor": None,
            }
        ]
    )
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(
        main.app,
        ["--json", "s3", "stat", "runs/step-100.safetensors", "--source", "source-1"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["key"] == "runs/step-100.safetensors"
    assert fake.calls == [
        (
            "GET",
            "/api/import-sources/source-1/browse",
            {"prefix": "runs/", "cursor": None, "page_size": 1000},
            None,
        )
    ]


def test_s3_tree_composes_browse_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(
        [
            {
                "prefixes": ["runs/checkpoints/"],
                "objects": [{"key": "runs/config.json", "size": 10}],
                "next_cursor": None,
            },
            {
                "prefixes": [],
                "objects": [
                    {"key": "runs/checkpoints/step-100.safetensors", "size": 42}
                ],
                "next_cursor": None,
            },
        ]
    )
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(
        main.app,
        [
            "--json",
            "s3",
            "tree",
            "runs/",
            "--source",
            "source-1",
            "--depth",
            "2",
            "--limit",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [node["key"] for node in payload["nodes"]] == [
        "runs/checkpoints/",
        "runs/config.json",
        "runs/checkpoints/step-100.safetensors",
    ]
    assert [call[1] for call in fake.calls] == [
        "/api/import-sources/source-1/browse",
        "/api/import-sources/source-1/browse",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["project", "list", "--cursor", "next"],
        ["job", "list", "--limit", "10"],
        ["dataset", "items", "version-1", "--tag", "portrait"],
        ["run", "list", "--limit", "10"],
        ["gallery", "list", "--run", "run-1"],
        ["lineage", "show", "asset:asset-1", "--depth", "3"],
        [
            "import",
            "preview",
            "--project",
            "project-1",
            "--prefix",
            "runs/",
            "--source",
            "source-1",
        ],
    ],
)
def test_unsupported_filter_flags_are_not_silently_sent(
    fake_client: FakeClient,
    arguments: list[str],
) -> None:
    result = runner.invoke(main.app, arguments)

    assert result.exit_code == 2
    assert fake_client.calls == []
