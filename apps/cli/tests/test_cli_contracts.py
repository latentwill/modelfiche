from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest
from typer.testing import CliRunner

from titles_cli import main


runner = CliRunner()


def test_public_cli_name_is_mfiche_and_legacy_name_is_absent() -> None:
    result = runner.invoke(main.app, ["--help"])

    assert result.exit_code == 0, result.output
    assert main.app.info.name == "mfiche"
    assert "Usage: mfiche " in result.output
    assert "Usage: titles " not in result.output



def test_unsupported_guidance_uses_mfiche() -> None:
    result = runner.invoke(main.app, ["import", "refresh", "asset:asset-1"])

    assert result.exit_code == 5, result.output
    assert "mfiche run refresh <run>" in result.output
    assert "titles run refresh <run>" not in result.output

class FakeClient:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, Any]] = []
        self.responses = list(responses or [])

    def _response(self) -> Any:
        return self.responses.pop(0) if self.responses else {"ok": True}

    def get(self, path: str, **params: Any) -> Any:
        self.calls.append(("GET", path, params, None))
        return self._response()

    def get_bytes(self, path: str, **params: Any) -> bytes:
        self.calls.append(("GET", path, params, None))
        value = self._response()
        return value if isinstance(value, bytes) else bytes(value)

    def get_text(self, path: str, **params: Any) -> str:
        self.calls.append(("GET", path, params, None))
        value = self._response()
        return value if isinstance(value, str) else str(value)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        self.calls.append(("POST", path, params, body))
        return self._response()
    def patch(self, path: str, body: Any = None) -> Any:
        self.calls.append(("PATCH", path, None, body))
        return self._response()

    def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path, None, None))
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

def test_unsupported_import_refresh_fails_before_http(fake_client: FakeClient) -> None:
    result = runner.invoke(main.app, ["--json", "import", "refresh", "dataset:dataset-1"])

    assert result.exit_code == 5
    assert json.loads(result.output)["error"]["code"] == "UNSUPPORTED_OPERATION"
    assert fake_client.calls == []


def test_removed_comment_update_is_not_advertised() -> None:
    result = runner.invoke(main.app, ["comment", "--help"])

    assert result.exit_code == 0, result.output
    assert "update" not in result.output


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


def test_wandb_credentials_use_public_key_contract(fake_client: FakeClient) -> None:
    commands = [
        (
            ["wandb", "credential", "register", "--alias", "agent", "--public-key", "ed25519-public-key"],
            ("POST", "/api/integrations/wandb/credentials", {}, {"alias": "agent", "public_key": "ed25519-public-key", "project_id": None}),
        ),
        (
            ["wandb", "credential", "list", "--project-id", "project-1"],
            ("GET", "/api/integrations/wandb/credentials", {"project_id": "project-1"}, None),
        ),
        (
            ["wandb", "credential", "rotate", "credential-1", "--public-key", "rotated-public-key"],
            ("POST", "/api/integrations/wandb/credentials/credential-1/rotate", {}, {"public_key": "rotated-public-key"}),
        ),
        (
            ["wandb", "credential", "revoke", "credential-1"],
            ("POST", "/api/integrations/wandb/credentials/credential-1/revoke", {}, None),
        ),
    ]
    for arguments, expected in commands:
        result = runner.invoke(main.app, ["--json", *arguments])
        assert result.exit_code == 0, result.output
        assert fake_client.calls[-1] == expected


def test_training_launch_input_rejects_secret_fields(
    fake_client: FakeClient,
    tmp_path: Any,
) -> None:
    input_file = tmp_path / "launch.json"
    input_file.write_text(
        json.dumps({"private_key": "never-send", "dataset_version_id": "version-1"}),
        encoding="utf-8",
    )
    result = runner.invoke(main.app, ["--json", "training-launch", "create", "--input", str(input_file)])
    assert result.exit_code == 2
    assert "forbidden secret fields" in result.output
    assert fake_client.calls == []


def test_wandb_credential_cli_never_exposes_private_key_option() -> None:
    result = runner.invoke(main.app, ["wandb", "credential", "register", "--help"])
    assert result.exit_code == 0, result.output
    assert "--private-key" not in result.output
    assert "--password-manager" not in result.output


def test_training_launch_flags_send_json_friendly_payload(fake_client: FakeClient) -> None:
    result = runner.invoke(
        main.app,
        [
            "--json",
            "training-launch",
            "create",
            "--dataset-version-id",
            "version-1",
            "--wandb-credential-id",
            "credential-1",
            "--source-id",
            "source-1",
            "--client-request-id",
            "request-1",
            "--name",
            "run-1",
            "--base-model",
            "model-1",
            "--output-directory",
            "/workspace/output",
            "--checkpoint-policy",
            '{"every_n_steps":100}',
            "--backup-policy",
            '{"prefix":"checkpoints"}',
            "--supported-endpoint-ids",
            '["endpoint-1","endpoint-2"]',
            "--expected-duration-seconds",
            "900",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_client.calls == [
        (
            "POST",
            "/api/training-launches",
            {},
            {
                "dataset_version_id": "version-1",
                "wandb_credential_id": "credential-1",
                "source_id": "source-1",
                "client_request_id": "request-1",
                "name": "run-1",
                "trainer": "ai-toolkit",
                "base_model": "model-1",
                "output_directory": "/workspace/output",
                "checkpoint_policy": {"every_n_steps": 100},
                "backup_policy": {"prefix": "checkpoints"},
                "supported_endpoint_ids": ["endpoint-1", "endpoint-2"],
                "expected_duration_seconds": 900,
            },
        )
    ]


def test_training_launch_input_json_and_status_use_api_contract(
    fake_client: FakeClient,
    tmp_path: Any,
) -> None:
    request = {
        "dataset_version_id": "version-1",
        "wandb_credential_id": "credential-1",
        "source_id": "source-1",
        "client_request_id": "request-1",
        "name": "run-1",
        "base_model": "model-1",
        "output_directory": "/workspace/output",
        "checkpoint_policy": {"every_n_steps": 100},
        "backup_policy": {},
        "supported_endpoint_ids": [],
        "expected_duration_seconds": 1200,
    }
    input_file = tmp_path / "launch.json"
    input_file.write_text(json.dumps(request), encoding="utf-8")
    result = runner.invoke(main.app, ["--json", "training-launch", "create", "--input", str(input_file)])
    assert result.exit_code == 0, result.output
    assert fake_client.calls[-1] == ("POST", "/api/training-launches", {}, {**request, "trainer": "ai-toolkit"})

    result = runner.invoke(main.app, ["--json", "training-launch", "status", "launch-1"])
    assert result.exit_code == 0, result.output
    assert fake_client.calls[-1] == ("GET", "/api/training-launches/launch-1", {}, None)


def test_training_launch_manifest_and_environment_preserve_output_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    fake = FakeClient([b'{"schema_version":"v1","unicode":"caf\xc3\xa9"}', "export WANDB_MODE=online\n"])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)
    manifest_path = tmp_path / "nested" / "manifest.json"
    environment_path = tmp_path / "nested" / "environment.sh"

    result = runner.invoke(
        main.app,
        ["--json", "training-launch", "manifest", "launch-1", "--output", str(manifest_path)],
    )
    assert result.exit_code == 0, result.output
    assert manifest_path.read_bytes() == b'{"schema_version":"v1","unicode":"caf\xc3\xa9"}'
    assert fake.calls[-1] == ("GET", "/api/training-launches/launch-1/manifest", {}, None)

    result = runner.invoke(
        main.app,
        ["--json", "training-launch", "environment", "launch-1", "--output", str(environment_path)],
    )
    assert result.exit_code == 0, result.output
    assert environment_path.read_bytes() == b"export WANDB_MODE=online\n"
    assert fake.calls[-1] == ("GET", "/api/training-launches/launch-1/environment", {}, None)


def test_training_launch_raw_commands_print_without_reserializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = b'{"unicode":"caf\xc3\xa9"}'
    environment = "export WANDB_MODE=online\n"
    fake = FakeClient([manifest, environment])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    manifest_result = runner.invoke(main.app, ["training-launch", "manifest", "launch-1"])
    assert manifest_result.exit_code == 0, manifest_result.output
    assert manifest_result.stdout_bytes == manifest

    environment_result = runner.invoke(main.app, ["training-launch", "environment", "launch-1"])
    assert environment_result.exit_code == 0, environment_result.output
    assert environment_result.stdout_bytes == environment.encode("utf-8")


def test_wandb_key_helpers_keep_private_material_local(tmp_path: Any) -> None:
    private_key_path = tmp_path / "keys" / "wandb-signing.key"

    generated = runner.invoke(
        main.app,
        [
            "--json",
            "wandb",
            "key",
            "generate",
            "--private-key-output",
            str(private_key_path),
        ],
    )

    assert generated.exit_code == 0, generated.output
    result = json.loads(generated.output)
    private_key = private_key_path.read_text(encoding="ascii").strip()
    assert result == {
        "algorithm": "Ed25519",
        "public_key": result["public_key"],
        "private_key_path": str(private_key_path),
    }
    assert private_key not in generated.output
    assert os.stat(private_key_path).st_mode & 0o777 == 0o600

    recovered = runner.invoke(
        main.app,
        [
            "--json",
            "wandb",
            "key",
            "public",
            "--private-key-file",
            str(private_key_path),
        ],
    )
    assert recovered.exit_code == 0, recovered.output
    assert json.loads(recovered.output)["public_key"] == result["public_key"]

    duplicate = runner.invoke(
        main.app,
        [
            "--json",
            "wandb",
            "key",
            "generate",
            "--private-key-output",
            str(private_key_path),
        ],
    )
    assert duplicate.exit_code == 2
    assert private_key_path.read_text(encoding="ascii").strip() == private_key


def test_agent_environment_signs_canonical_claims_and_writes_mode_0600(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    private_key, public_key = main.generate_keypair()
    private_key_path = main.write_private_key(tmp_path / "wandb-signing.key", private_key)
    claims = {
        "claims_version": 1,
        "credential_id": "credential-1",
        "expires_at": "2026-07-14T00:00:00Z",
        "issued_at": "2026-07-13T00:00:00Z",
        "kid": "key-1",
        "launch_id": "launch-1",
        "project_id": "project-1",
        "run_id": "run-1",
        "wandb_run_id": "wandb-run-1",
        "workspace_id": "workspace-1",
    }
    template = (
        'export WANDB_API_KEY="<MF1_BASE32_ED25519_SIGNED_TOKEN>"\n'
        'export MODELFICHE_HANDOFF_TOKEN="$WANDB_API_KEY"\n'
    )
    fake = FakeClient(
        [
            {"id": "launch-1", "run_id": "run-1", "manifest": {"telemetry": {"token": {"claims": claims}}}},
            template,
        ]
    )
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)
    output = tmp_path / "secrets" / "training.env"

    rendered = runner.invoke(
        main.app,
        [
            "--json",
            "training-launch",
            "agent-environment",
            "launch-1",
            "--private-key-file",
            str(private_key_path),
            "--output",
            str(output),
        ],
    )

    assert rendered.exit_code == 0, rendered.output
    metadata = json.loads(rendered.output)
    assert metadata["output"] == str(output)
    assert metadata["mode"] == "0600"
    environment = output.read_text(encoding="utf-8")
    assert "<MF1_BASE32_ED25519_SIGNED_TOKEN>" not in environment
    token = environment.split('export WANDB_API_KEY="', 1)[1].split('"', 1)[0]
    assert token not in rendered.output
    prefix, encoded_payload, encoded_signature = token.split("_")
    assert prefix == "MF1"
    payload = base64.b32decode(encoded_payload + "=" * (-len(encoded_payload) % 8))
    signature = base64.b32decode(encoded_signature + "=" * (-len(encoded_signature) % 8))
    public_bytes = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
    Ed25519PublicKey.from_public_bytes(public_bytes).verify(signature, payload)
    assert json.loads(payload) == claims
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert fake.calls == [
        ("GET", "/api/training-launches/launch-1", {}, None),
        ("GET", "/api/training-launches/launch-1/environment", {}, None),
    ]


def test_training_launch_inspect_combines_launch_and_live_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch = {"id": "launch-1", "run_id": "run-1", "state": "running"}
    live = {"run_id": "run-1", "status": "running", "current_step": 42}
    fake = FakeClient([launch, live])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(main.app, ["--json", "training-launch", "inspect", "launch-1"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"launch": launch, "run": live}
    assert fake.calls == [
        ("GET", "/api/training-launches/launch-1", {}, None),
        ("GET", "/api/runs/run-1/live", {}, None),
    ]


def test_training_launch_wait_returns_terminal_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeClient(
        [
            {"id": "launch-1", "state": "running"},
            {"id": "launch-1", "state": "completed", "run_id": "run-1"},
        ]
    )
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    result = runner.invoke(
        main.app,
        [
            "--json",
            "--quiet",
            "training-launch",
            "wait",
            "launch-1",
            "--timeout",
            "10",
            "--poll-interval",
            "0.1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "completed"
    assert fake.calls == [
        ("GET", "/api/training-launches/launch-1", {}, None),
        ("GET", "/api/training-launches/launch-1", {}, None),
    ]


def test_run_live_and_metrics_use_agent_read_contract(fake_client: FakeClient) -> None:
    commands = [
        (
            ["run", "live", "run-1"],
            ("GET", "/api/runs/run-1/live", {}, None),
        ),
        (
            ["run", "metrics", "run-1", "--name", "loss/denoise"],
            ("GET", "/api/runs/run-1/metrics", {"name": "loss/denoise"}, None),
        ),
    ]

    for arguments, expected in commands:
        result = runner.invoke(main.app, ["--json", *arguments])
        assert result.exit_code == 0, result.output
        assert fake_client.calls[-1] == expected


@pytest.mark.parametrize("state", ["failed", "interrupted"])
def test_training_launch_wait_returns_exit_7_for_unsuccessful_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    fake = FakeClient([{"id": "launch-1", "state": state}])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(
        main.app,
        ["--json", "--quiet", "training-launch", "wait", "launch-1"],
    )

    assert result.exit_code == 7
    assert json.loads(result.output)["state"] == state


def test_training_prepare_bundles_self_contained_checkpoint_sync(monkeypatch, tmp_path) -> None:
    packet = {
        "run_id": "run-1",
        "manifest": {
            "dataset": {"download_url": "/api/transfers/export-1/download"},
            "telemetry": {"token": {"claims": {"launch_id": "launch-1"}}},
        },
        "preflight": {"ready_to_start": True},
        "ai_toolkit": {"logging": {"use_wandb": False}},
    }
    fake = FakeClient([packet, 'export MODELFICHE_HANDOFF_TOKEN="<MF1_BASE32_ED25519_SIGNED_TOKEN>"\n', b"dataset-zip"])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)
    monkeypatch.setattr(main, "read_private_key", lambda _path: object())
    monkeypatch.setattr(main, "sign_launch_claims", lambda _claims, _key: "signed-token")
    key = tmp_path / "signing.key"
    key.write_text("private", encoding="utf-8")
    output = tmp_path / "packet"

    result = runner.invoke(
        main.app,
        ["--json", "training", "run", "prepare", "launch-1", "--output", str(output), "--private-key-file", str(key)],
    )

    assert result.exit_code == 0, result.output
    assert (output / "modelfiche-run-sync.py").read_text(encoding="utf-8") == main.Path(main.__file__).with_name("run_sync.py").read_text(encoding="utf-8")
    assert (output / "run-sync-command.txt").read_text(encoding="utf-8") == "python3 modelfiche-run-sync.py --manifest manifest.json\n"
    assert 'MODELFICHE_HANDOFF_TOKEN="signed-token"' in (output / "trainer.env").read_text(encoding="utf-8")
    assert (output / "training-dataset.zip").read_bytes() == b"dataset-zip"
    checksums = json.loads((output / "checksums.json").read_text(encoding="utf-8"))["files"]
    assert set(checksums) >= {"manifest.json", "trainer.env", "modelfiche-run-sync.py", "run-sync-command.txt", "training-dataset.zip"}


def test_cli_parity_mutations_use_supported_api_contracts(fake_client: FakeClient) -> None:
    commands = [
        (
            ["caption", "remove-word", "draft-1", "--word", "obsolete", "--all"],
            ("POST", "/api/dataset-drafts/draft-1/operations", {}, {"operation": "remove_word", "parameters": {"word": "obsolete"}, "item_ids": None, "tag": None, "included": None, "caption_format": None, "all": True}),
        ),
        (
            ["grid", "cancel", "eval-run-1"],
            ("POST", "/api/eval-runs/eval-run-1/cancel", {}, None),
        ),
        (
            ["asset", "delete", "asset-1", "--delete-content"],
            ("DELETE", "/api/assets/asset-1?report=true&delete_content=true", None, None),
        ),
        (
            ["workspace", "rename", "workspace-1", "--name", "Studio"],
            ("PATCH", "/api/workspaces/workspace-1", None, {"name": "Studio"}),
        ),
        (
            ["s3", "source-disconnect", "source-1"],
            ("DELETE", "/api/import-sources/source-1", None, None),
        ),
        (
            ["caption", "set-included", "draft-1", "--included", "--all"],
            ("POST", "/api/dataset-drafts/draft-1/operations", {}, {"operation": "set_included", "parameters": {}, "item_ids": None, "tag": None, "included": True, "caption_format": None, "all": True}),
        ),
        (
            ["caption", "format", "draft-1", "--value", "json"],
            ("PATCH", "/api/dataset-drafts/draft-1/caption-format", None, {"caption_format": "json"}),
        ),
        (
            ["caption", "undo", "draft-1", "operation-1"],
            ("POST", "/api/dataset-drafts/draft-1/operations/operation-1/undo", {}, None),
        ),
        (
            ["prompt-set", "generate", "--instruction", "Portrait prompts", "--count", "3", "--project", "project-1"],
            ("POST", "/api/prompt-sets/generate", {}, {"instruction": "Portrait prompts", "count": 3, "context": {"project_id": "project-1"}}),
        ),
    ]

    for arguments, expected in commands:
        result = runner.invoke(main.app, ["--json", *arguments])
        assert result.exit_code == 0, result.output
        assert fake_client.calls[-1] == expected


def test_local_import_encodes_supported_folder_files(monkeypatch, tmp_path) -> None:
    folder = tmp_path / "images"
    folder.mkdir()
    (folder / "one.png").write_bytes(b"png")
    (folder / "one.txt").write_text("caption", encoding="utf-8")
    (folder / "ignored.bin").write_bytes(b"ignored")
    fake = FakeClient()
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(main.app, ["--json", "import", "local", str(folder), "--project", "project-1", "--name", "Local set"])

    assert result.exit_code == 0, result.output
    body = fake.calls[0][3]
    assert fake.calls[0][:3] == ("POST", "/api/local-imports", {})
    assert body["project_id"] == "project-1"
    assert body["dataset_name"] == "Local set"
    assert [item["path"] for item in body["files"]] == ["one.png", "one.txt"]
    assert base64.b64decode(body["files"][0]["content_base64"]) == b"png"


def test_settings_update_accepts_nonsecret_json_patch(monkeypatch, tmp_path) -> None:
    patch_file = tmp_path / "settings.json"
    patch_file.write_text(json.dumps({"generation": {"prompt_prepend": "studio"}}), encoding="utf-8")
    fake = FakeClient()
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(main.app, ["--json", "settings", "update", "--file", str(patch_file)])

    assert result.exit_code == 0, result.output
    assert fake.calls == [("PATCH", "/api/operator-settings", None, {"generation": {"prompt_prepend": "studio"}})]


def test_image_generate_dry_run_compiles_without_admitting(monkeypatch) -> None:
    fake = FakeClient([
        {"id": "version-1", "project_id": "project-1", "model_id": "model-1", "checkpoint_revision_id": "revision-1"},
        {"kind": "compiled_generation", "admission": {"id": "admission-1", "state": "ready", "version": 0}},
    ])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(main.app, ["--json", "image", "generate", "--model-version", "version-1", "--prompt", "A portrait", "--param", "num_images=1", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert [call[:2] for call in fake.calls] == [
        ("GET", "/api/model-versions/version-1"),
        ("POST", "/api/generation-requests/compile"),
    ]
    assert fake.calls[1][3]["workflow"] == "image"
    assert fake.calls[1][3]["prompt"] == "A portrait"


def test_checkpoint_verify_reports_persisted_evidence(monkeypatch) -> None:
    fake = FakeClient([{"artifact": {"asset_id": "asset-1", "checkpoint_revision_id": "revision-1", "sha256": "a" * 64, "storage": [{"provider": "s3", "size": 42, "verification_state": "verified"}]}}])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(main.app, ["--json", "checkpoint", "verify", "checkpoint-1"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["verified"] is True
    assert fake.calls == [("GET", "/api/checkpoints/checkpoint-1", {}, None)]


def test_lineage_path_follows_directed_edges(monkeypatch) -> None:
    first = {"source_type": "asset", "source_id": "asset-1", "relationship": "input_to", "target_type": "run", "target_id": "run-1"}
    second = {"source_type": "run", "source_id": "run-1", "relationship": "produced", "target_type": "checkpoint", "target_id": "checkpoint-1"}
    fake = FakeClient([[first], [second]])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(main.app, ["--json", "lineage", "path", "asset:asset-1", "checkpoint:checkpoint-1"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["depth"] == 2
    assert payload["edges"] == [first, second]
