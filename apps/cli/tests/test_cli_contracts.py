from __future__ import annotations

import base64
import io
import json
import os
import zipfile
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import httpx
import pytest
from typer.testing import CliRunner

from titles_cli import main
from titles_cli.client import ApiError, TitlesClient


runner = CliRunner()


def test_public_cli_name_is_mfiche_and_legacy_name_is_absent() -> None:
    result = runner.invoke(main.app, ["--help"])

    assert result.exit_code == 0, result.output
    assert main.app.info.name == "mfiche"
    assert "Usage: mfiche " in result.output
    assert "Usage: titles " not in result.output


def test_remote_client_sends_configured_service_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TITLES_REMOTE_ACCESS_ORIGIN", "https://modelfiche.example.com")
    monkeypatch.setenv("TITLES_REMOTE_ACCESS_CLIENT_ID", "remote-client")
    monkeypatch.setenv("TITLES_REMOTE_ACCESS_CLIENT_SECRET", "remote-secret")

    client = TitlesClient(base_url="https://modelfiche.example.com")
    try:
        assert client.http.headers["Origin"] == "https://modelfiche.example.com"
        assert client.http.headers["CF-Access-Client-Id"] == "remote-client"
        assert client.http.headers["CF-Access-Client-Secret"] == "remote-secret"
    finally:
        client.close()


def test_local_client_does_not_send_remote_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TITLES_REMOTE_ACCESS_ORIGIN", "https://modelfiche.example.com")
    monkeypatch.setenv("TITLES_REMOTE_ACCESS_CLIENT_ID", "remote-client")
    monkeypatch.setenv("TITLES_REMOTE_ACCESS_CLIENT_SECRET", "remote-secret")

    client = TitlesClient(base_url="http://127.0.0.1:8400")
    try:
        assert client.http.headers["Origin"] == "http://127.0.0.1:5173"
        assert "CF-Access-Client-Id" not in client.http.headers
        assert "CF-Access-Client-Secret" not in client.http.headers
    finally:
        client.close()


def test_remote_client_fails_closed_on_incomplete_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TITLES_REMOTE_ACCESS_ORIGIN", "https://modelfiche.example.com")
    monkeypatch.setenv("TITLES_REMOTE_ACCESS_CLIENT_ID", "remote-client")

    with pytest.raises(ApiError, match="TITLES_REMOTE_ACCESS_CLIENT_SECRET"):
        TitlesClient(base_url="https://modelfiche.example.com")


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

def test_kef_krea2_launch_preserves_editable_training_settings(
    fake_client: FakeClient,
) -> None:
    fake_client.responses.extend(
        [
            [{"id": "dataset-1", "name": "Lotus Rocks", "current_version_id": "version-1"}],
            [{"id": "version-1", "name": "v1", "version_number": 1}],
            {
                "credentials": [{"id": "credential-1"}],
                "sources": [{"id": "source-1", "verified": True}],
            },
            {"id": "launch-1"},
        ]
    )

    result = runner.invoke(
        main.app,
        [
            "--json",
            "training",
            "run",
            "create",
            "--dataset",
            "Lotus Rocks",
            "--name",
            "lotus-kef-v2",
            "--trainer",
            "kef-krea2",
            "--base-model",
            "krea/Krea-2-Raw",
            "--steps",
            "2400",
            "--tokens",
            "9",
            "--gradient-accumulation",
            "3",
            "--resolution",
            "768",
            "--no-compile-transformer",
            "--sample-interval",
            "250",
            "--sample-steps",
            "16",
            "--caption-mode",
            "random",
        ],
    )

    assert result.exit_code == 0, result.output
    request = next(
        body
        for method, path, _, body in fake_client.calls
        if method == "POST" and path == "/api/training-launches"
    )
    assert request["training_config"]["steps"] == 2400
    assert request["training_config"]["num_tokens"] == 9
    assert request["training_config"]["gradient_accumulation"] == 3
    assert request["training_config"]["resolution"] == 768
    assert request["training_config"]["compile_transformer"] is False
    assert request["training_config"]["sample_interval"] == 250
    assert request["training_config"]["sample_steps"] == 16
    assert request["training_config"]["caption_mode"] == "random"

def test_kef_krea2_launch_resolves_dataset_id_outside_first_listing_page(
    fake_client: FakeClient,
) -> None:
    dataset_id = "d97b2bea-b4d5-4bdc-973c-6ff71c70e71d"
    fake_client.responses.extend(
        [
            {"id": dataset_id, "name": "Lotus Rocks"},
            [{"id": "version-2", "name": "v2", "version_number": 2}],
            {
                "credentials": [{"id": "credential-1"}],
                "sources": [{"id": "source-1", "verified": True}],
            },
            {"id": "launch-1"},
        ]
    )

    result = runner.invoke(
        main.app,
        [
            "--json",
            "training",
            "run",
            "create",
            "--dataset",
            dataset_id,
            "--version",
            "2",
            "--name",
            "lotus-kef-v2",
            "--trainer",
            "kef-krea2",
            "--base-model",
            "krea/Krea-2-Raw",
        ],
    )

    assert result.exit_code == 0, result.output
    assert fake_client.calls[0] == (
        "GET",
        f"/api/datasets/{dataset_id}",
        {},
        None,
    )



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
    result = runner.invoke(
        main.app, ["--json", "import", "backfill-metrics", "--run", "run-1"]
    )

    assert result.exit_code == 0, result.output
    assert fake_client.calls == [("POST", "/api/runs/run-1/refresh", {}, None)]


def test_unsupported_import_refresh_fails_before_http(fake_client: FakeClient) -> None:
    result = runner.invoke(
        main.app, ["--json", "import", "refresh", "dataset:dataset-1"]
    )

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


def test_wandb_credentials_use_one_global_public_key_contract(
    fake_client: FakeClient,
) -> None:
    registered = runner.invoke(
        main.app,
        [
            "--json",
            "wandb",
            "credential",
            "register",
            "--public-key",
            "ed25519-public-key",
        ],
    )
    assert registered.exit_code == 0, registered.output
    assert fake_client.calls[-1] == (
        "POST",
        "/api/integrations/wandb/credentials",
        {},
        {"alias": "modelfiche", "public_key": "ed25519-public-key"},
    )

    listed = runner.invoke(
        main.app, ["--json", "wandb", "credential", "list"]
    )
    assert listed.exit_code == 0, listed.output
    assert fake_client.calls[-1] == (
        "GET",
        "/api/integrations/wandb/credentials",
        {},
        None,
    )

    fake_client.responses.extend(
        [[{"id": "credential-1"}], {"id": "credential-1", "state": "active"}]
    )
    rotated = runner.invoke(
        main.app,
        [
            "--json",
            "wandb",
            "credential",
            "rotate",
            "--public-key",
            "rotated-public-key",
        ],
    )
    assert rotated.exit_code == 0, rotated.output
    assert fake_client.calls[-2:] == [
        ("GET", "/api/integrations/wandb/credentials", {}, None),
        (
            "POST",
            "/api/integrations/wandb/credentials/credential-1/rotate",
            {},
            {"public_key": "rotated-public-key"},
        ),
    ]

    fake_client.responses.extend(
        [[{"id": "credential-1"}], {"id": "credential-1", "state": "revoked"}]
    )
    revoked = runner.invoke(
        main.app, ["--json", "wandb", "credential", "revoke"]
    )
    assert revoked.exit_code == 0, revoked.output
    assert fake_client.calls[-2:] == [
        ("GET", "/api/integrations/wandb/credentials", {}, None),
        (
            "POST",
            "/api/integrations/wandb/credentials/credential-1/revoke",
            {},
            None,
        ),
    ]


def test_training_launch_input_rejects_secret_fields(
    fake_client: FakeClient,
    tmp_path: Any,
) -> None:
    input_file = tmp_path / "launch.json"
    input_file.write_text(
        json.dumps({"private_key": "never-send", "dataset_version_id": "version-1"}),
        encoding="utf-8",
    )
    result = runner.invoke(
        main.app, ["--json", "training-launch", "create", "--input", str(input_file)]
    )
    assert result.exit_code == 2
    assert "forbidden secret fields" in result.output
    assert fake_client.calls == []


def test_wandb_credential_cli_exposes_only_global_public_key_controls() -> None:
    register = runner.invoke(
        main.app, ["wandb", "credential", "register", "--help"]
    )
    rotate = runner.invoke(main.app, ["wandb", "credential", "rotate", "--help"])
    revoke = runner.invoke(main.app, ["wandb", "credential", "revoke", "--help"])

    assert register.exit_code == rotate.exit_code == revoke.exit_code == 0
    assert "--private-key" not in register.output
    assert "--password-manager" not in register.output
    assert "--alias" not in register.output
    assert "CREDENTIAL" not in rotate.output
    assert "CREDENTIAL" not in revoke.output


def test_training_launch_flags_send_json_friendly_payload(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(
        main.app,
        [
            "--json",
            "training-launch",
            "create",
            "--dataset-version-id",
            "version-1",
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
    result = runner.invoke(
        main.app, ["--json", "training-launch", "create", "--input", str(input_file)]
    )
    assert result.exit_code == 0, result.output
    assert fake_client.calls[-1] == (
        "POST",
        "/api/training-launches",
        {},
        {**request, "trainer": "ai-toolkit"},
    )

    result = runner.invoke(
        main.app, ["--json", "training-launch", "status", "launch-1"]
    )
    assert result.exit_code == 0, result.output
    assert fake_client.calls[-1] == ("GET", "/api/training-launches/launch-1", {}, None)


def test_training_launch_manifest_and_environment_preserve_output_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    fake = FakeClient(
        [
            b'{"schema_version":"v1","unicode":"caf\xc3\xa9"}',
            "export WANDB_MODE=online\n",
        ]
    )
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)
    manifest_path = tmp_path / "nested" / "manifest.json"
    environment_path = tmp_path / "nested" / "environment.sh"

    result = runner.invoke(
        main.app,
        [
            "--json",
            "training-launch",
            "manifest",
            "launch-1",
            "--output",
            str(manifest_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (
        manifest_path.read_bytes() == b'{"schema_version":"v1","unicode":"caf\xc3\xa9"}'
    )
    assert fake.calls[-1] == (
        "GET",
        "/api/training-launches/launch-1/manifest",
        {},
        None,
    )

    result = runner.invoke(
        main.app,
        [
            "--json",
            "training-launch",
            "environment",
            "launch-1",
            "--output",
            str(environment_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert environment_path.read_bytes() == b"export WANDB_MODE=online\n"
    assert fake.calls[-1] == (
        "GET",
        "/api/training-launches/launch-1/environment",
        {},
        None,
    )


def test_training_launch_raw_commands_print_without_reserializing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = b'{"unicode":"caf\xc3\xa9"}'
    environment = "export WANDB_MODE=online\n"
    fake = FakeClient([manifest, environment])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    manifest_result = runner.invoke(
        main.app, ["training-launch", "manifest", "launch-1"]
    )
    assert manifest_result.exit_code == 0, manifest_result.output
    assert manifest_result.stdout_bytes == manifest

    environment_result = runner.invoke(
        main.app, ["training-launch", "environment", "launch-1"]
    )
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
    private_key_path = main.write_private_key(
        tmp_path / "wandb-signing.key", private_key
    )
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
            {
                "id": "launch-1",
                "run_id": "run-1",
                "manifest": {"telemetry": {"token": {"claims": claims}}},
            },
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
    signature = base64.b32decode(
        encoded_signature + "=" * (-len(encoded_signature) % 8)
    )
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

    result = runner.invoke(
        main.app, ["--json", "training-launch", "inspect", "launch-1"]
    )

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


@pytest.mark.parametrize(
    ("trainer_id", "trainer_config_file"),
    [
        ("ai-toolkit", "ai-toolkit-logging.yaml"),
        ("kef-krea2", "kef-krea2-training.json"),
    ],
)
def test_training_prepare_bundles_self_contained_checkpoint_sync(
    monkeypatch, tmp_path, trainer_id, trainer_config_file
) -> None:
    packet = {
        "run_id": "run-1",
        "manifest": {
            "dataset": {"download_url": "http://ingress.test/api/transfers/export-1/download"},
            "run": {"trainer": trainer_id},
            "telemetry": {"token": {"claims": {"launch_id": "launch-1"}}},
        },
        "preflight": {"ready_to_start": True},
        "trainer": {
            "id": trainer_id,
            "telemetry": {"enabled": trainer_id == "kef-krea2"},
            "configuration": {"steps": 2400, "num_tokens": 7, "concept_type": "style"},
            "command": {
                "argv": [
                    "kef-krea2-train",
                    "training-dataset",
                    "/workspace/output",
                    "--steps",
                    "2400",
                ],
                "working_directory": ".",
            },
        },
        "ai_toolkit": {"logging": {"use_wandb": False}},
    }
    dataset_archive = b"dataset-zip"
    if trainer_id == "kef-krea2":
        source = io.BytesIO()
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "assets": [{"id": "asset-1", "name": "rock.png"}],
                        "dataset_items": [
                            {
                                "id": "item-1",
                                "asset_id": "asset-1",
                                "caption": "pale porous stone",
                                "included": True,
                                "position": 0,
                            }
                        ],
                    }
                ),
            )
            archive.writestr("files/asset-1/rock.png", b"image")
        dataset_archive = source.getvalue()
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host != "operator.test":
            return httpx.Response(404)
        if request.url.path == "/api/training-launches/launch-1/packet":
            return httpx.Response(200, json=packet)
        if request.url.path == "/api/training-launches/launch-1/environment":
            return httpx.Response(200, text='export MODELFICHE_HANDOFF_TOKEN="<MF1_BASE32_ED25519_SIGNED_TOKEN>"\n')
        if request.url.path == "/api/transfers/export-1/download":
            return httpx.Response(200, content=dataset_archive)
        return httpx.Response(404)

    client = TitlesClient(base_url="http://operator.test")
    client.http.close()
    client.http = httpx.Client(base_url="http://operator.test", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(main.Runtime, "client", lambda self: client)
    monkeypatch.setattr(main, "read_private_key", lambda _path: object())
    monkeypatch.setattr(
        main, "sign_launch_claims", lambda _claims, _key: "signed-token"
    )
    key = tmp_path / "signing.key"
    key.write_text("private", encoding="utf-8")
    output = tmp_path / "packet"

    result = runner.invoke(
        main.app,
        [
            "--json",
            "training",
            "run",
            "prepare",
            "launch-1",
            "--output",
            str(output),
            "--private-key-file",
            str(key),
        ],
    )

    assert result.exit_code == 0, result.output
    assert 'MODELFICHE_HANDOFF_TOKEN="signed-token"' in (
        output / "trainer.env"
    ).read_text(encoding="utf-8")
    if trainer_id == "ai-toolkit":
        assert (output / "training-dataset.zip").read_bytes() == dataset_archive
    else:
        with zipfile.ZipFile(output / "training-dataset.zip") as archive:
            assert archive.read("images/asset-1/rock.png") == b"image"
            rows = [
                json.loads(line)
                for line in archive.read("manifest.final.jsonl").decode().splitlines()
            ]
        assert rows == [
            {
                "caption": "pale porous stone",
                "path": "images/asset-1/rock.png",
                "sha256": "6105d6cc76af400325e94d588ce511be5bfdbb73b437dc51eca43917d7a43e3d",
                "provenance": {
                    "asset_id": "asset-1",
                    "dataset_item_id": "item-1",
                    "original_caption": "pale porous stone",
                },
                "caption_role": "content",
                "source_type": "modelfiche",
                "split": "train",
            }
        ]
    assert (output / trainer_config_file).is_file()
    if trainer_id == "kef-krea2":
        assert (output / "kef-krea2-telemetry.json").is_file()
        training = json.loads(
            (output / "kef-krea2-training.json").read_text(encoding="utf-8")
        )
        assert training["configuration"]["num_tokens"] == 7
        assert training["command"]["argv"][-1] == "2400"
    checksums = json.loads((output / "checksums.json").read_text(encoding="utf-8"))[
        "files"
    ]
    assert set(checksums) >= {
        "manifest.json",
        "trainer.env",
        "modelfiche-run-sync.py",
        "run-sync-command.txt",
        "training-dataset.zip",
        trainer_config_file,
    }


def test_cli_parity_mutations_use_supported_api_contracts(
    fake_client: FakeClient,
) -> None:
    commands = [
        (
            ["caption", "remove-word", "draft-1", "--word", "obsolete", "--all"],
            (
                "POST",
                "/api/dataset-drafts/draft-1/operations",
                {},
                {
                    "operation": "remove_word",
                    "parameters": {"word": "obsolete"},
                    "item_ids": None,
                    "tag": None,
                    "included": None,
                    "caption_format": None,
                    "all": True,
                },
            ),
        ),
        (
            ["asset", "delete", "asset-1", "--delete-content"],
            (
                "DELETE",
                "/api/assets/asset-1?report=true&delete_content=true",
                None,
                None,
            ),
        ),
        (
            ["asset", "repair-images", "--project", "project-1", "--no-wait"],
            (
                "POST",
                "/api/jobs",
                {},
                {
                    "kind": "image.repair_lineage",
                    "payload": {"project_id": "project-1"},
                },
            ),
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
            (
                "POST",
                "/api/dataset-drafts/draft-1/operations",
                {},
                {
                    "operation": "set_included",
                    "parameters": {},
                    "item_ids": None,
                    "tag": None,
                    "included": True,
                    "caption_format": None,
                    "all": True,
                },
            ),
        ),
        (
            ["caption", "format", "draft-1", "--value", "json"],
            (
                "PATCH",
                "/api/dataset-drafts/draft-1/caption-format",
                None,
                {"caption_format": "json"},
            ),
        ),
        (
            ["caption", "undo", "draft-1", "operation-1"],
            (
                "POST",
                "/api/dataset-drafts/draft-1/operations/operation-1/undo",
                {},
                None,
            ),
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

    result = runner.invoke(
        main.app,
        [
            "--json",
            "import",
            "local",
            str(folder),
            "--project",
            "project-1",
            "--name",
            "Local set",
        ],
    )

    assert result.exit_code == 0, result.output
    body = fake.calls[0][3]
    assert fake.calls[0][:3] == ("POST", "/api/local-imports", {})
    assert body["project_id"] == "project-1"
    assert body["dataset_name"] == "Local set"
    assert [item["path"] for item in body["files"]] == ["one.png", "one.txt"]
    assert base64.b64decode(body["files"][0]["content_base64"]) == b"png"


def test_settings_update_accepts_nonsecret_json_patch(monkeypatch, tmp_path) -> None:
    patch_file = tmp_path / "settings.json"
    patch_file.write_text(
        json.dumps({"generation": {"prompt_prepend": "studio"}}), encoding="utf-8"
    )
    fake = FakeClient()
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(
        main.app, ["--json", "settings", "update", "--file", str(patch_file)]
    )

    assert result.exit_code == 0, result.output
    assert fake.calls == [
        (
            "PATCH",
            "/api/operator-settings",
            None,
            {"generation": {"prompt_prepend": "studio"}},
        )
    ]


def test_image_generate_dry_run_compiles_without_admitting(monkeypatch) -> None:
    fake = FakeClient(
        [
            {
                "id": "version-1",
                "project_id": "project-1",
                "model_id": "model-1",
                "checkpoint_revision_id": "revision-1",
            },
            {
                "kind": "compiled_generation",
                "admission": {"id": "admission-1", "state": "ready", "version": 0},
            },
        ]
    )
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(
        main.app,
        [
            "--json",
            "image",
            "generate",
            "--model-version",
            "version-1",
            "--prompt",
            "A portrait",
            "--param",
            "num_images=1",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [call[:2] for call in fake.calls] == [
        ("GET", "/api/model-versions/version-1"),
        ("POST", "/api/generation-requests/compile"),
    ]
    assert fake.calls[1][3]["workflow"] == "image"
    assert fake.calls[1][3]["prompt"] == "A portrait"


def test_checkpoint_verify_reports_persisted_evidence(monkeypatch) -> None:
    fake = FakeClient(
        [
            {
                "artifact": {
                    "asset_id": "asset-1",
                    "checkpoint_revision_id": "revision-1",
                    "sha256": "a" * 64,
                    "storage": [
                        {"provider": "s3", "size": 42, "verification_state": "verified"}
                    ],
                }
            }
        ]
    )
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(main.app, ["--json", "checkpoint", "verify", "checkpoint-1"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["verified"] is True
    assert fake.calls == [("GET", "/api/checkpoints/checkpoint-1", {}, None)]


def test_lineage_path_follows_directed_edges(monkeypatch) -> None:
    first = {
        "source_type": "asset",
        "source_id": "asset-1",
        "relationship": "input_to",
        "target_type": "run",
        "target_id": "run-1",
    }
    second = {
        "source_type": "run",
        "source_id": "run-1",
        "relationship": "produced",
        "target_type": "checkpoint",
        "target_id": "checkpoint-1",
    }
    fake = FakeClient([[first], [second]])
    monkeypatch.setattr(main.Runtime, "client", lambda self: fake)

    result = runner.invoke(
        main.app,
        ["--json", "lineage", "path", "asset:asset-1", "checkpoint:checkpoint-1"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["depth"] == 2
    assert payload["edges"] == [first, second]


def test_plan_helpers_build_inline_cases_and_digest() -> None:
    from titles_cli.main import build_plan

    plan = build_plan(
        project="project-1",
        prompt=["a prompt"],
        prompt_file=None,
        target=['{"provider":"fal","endpoint_id":"ideogram/v4/lora"}'],
        x_axis="guidance_scale",
        x_value=["5", "7"],
        y_axis="aspect_ratio",
        shared=["num_images=1"],
        y_value=["1:1"],
        target_override=['{"seed":42}'],
    )
    assert plan["contract_version"] == "2026-07-22.v1"
    assert plan["cases"][0]["input"]["prompt"] == "a prompt"
    assert plan["cases"][0]["input_digest"].startswith("sha256:")
    assert plan["targets"][0]["overrides"] == {"seed": 42}


def test_grid_command_uses_project_scoped_preflight_route(
    fake_client: FakeClient,
) -> None:
    result = runner.invoke(
        main.app,
        [
            "--json",
            "grid",
            "preflight",
            "--project",
            "project-1",
            "--prompt",
            "a prompt",
            "--target",
            "fal:ideogram/v4/lora",
            "--x-axis",
            "guidance_scale",
            "--x-value",
            "5",
            "--y-axis",
            "aspect_ratio",
            "--y-value",
            "1:1",
            "--idempotency-key",
            "idem-1",
        ],
    )
    assert result.exit_code == 0, result.output
    method, path, params, body = fake_client.calls[0]
    assert method == "POST"
    assert path == "/api/projects/project-1/experiment-plans/preflight"
    assert params == {}
    assert body["idempotency_key"] == "idem-1"
    assert body["cases"][0]["input"]["prompt"] == "a prompt"
    assert body["targets"][0]["endpoint_id"] == "ideogram/v4/lora"


def test_prompt_set_new_writes_are_removed() -> None:
    result = runner.invoke(main.app, ["prompt-set", "--help"])
    assert result.exit_code == 0, result.output
    assert "list" in result.output
    assert "show" in result.output
    assert "create" not in result.output
    assert "version" not in result.output


def test_request_plan_strips_authoritative_schema_and_defaults() -> None:
    from titles_cli.plan import request_plan

    payload = request_plan(
        {
            "contract_version": "2026-07-22.v1",
            "axes": {
                "x": {
                    "name": "target",
                    "values": [{"value": "a", "digest": "sha256:x"}],
                },
                "y": {
                    "name": "prompt",
                    "values": [{"value": "b", "digest": "sha256:y"}],
                },
                "z": None,
            },
            "cases": [
                {
                    "case_id": "c",
                    "ordinal": 0,
                    "input": {"prompt": "p"},
                    "input_digest": "sha256:z",
                }
            ],
            "targets": [
                {
                    "target_id": "t",
                    "ordinal": 0,
                    "provider": "fal",
                    "endpoint_id": "ideogram/v4/lora",
                    "schema": {"type": "object"},
                    "schema_digest": "sha256:s",
                    "target_overrides": {"seed": 4},
                }
            ],
            "shared_params": {"num_images": 1},
            "endpoint_defaults": {"seed": 1},
        }
    )
    assert payload["targets"][0]["overrides"] == {"seed": 4}
    assert "schema" not in payload["targets"][0]
    assert "schema_digest" not in payload["targets"][0]
    assert "endpoint_defaults" not in payload


def test_two_targets_and_cases_require_axes() -> None:
    from titles_cli.main import build_plan

    plan = build_plan(
        project="p",
        prompt=["one", "two"],
        prompt_file=None,
        target=["fal:ideogram/v4/lora", "fal:fal-ai/krea-2/turbo/lora"],
        x_axis="target",
        x_value=["0", "1"],
        y_axis="prompt",
        y_value=["0", "1"],
    )
    assert len(plan["targets"]) == 2
    assert len(plan["cases"]) == 2


def test_grid_queue_requires_billing_acknowledgement(fake_client: FakeClient) -> None:
    rejected = runner.invoke(
        main.app,
        [
            "--json",
            "grid",
            "queue",
            "--project",
            "project-1",
            "grid-1",
            "--plan-version",
            "1",
            "--plan-digest",
            "sha256:x",
            "--idempotency-key",
            "queue-1",
        ],
    )
    assert rejected.exit_code == 2
    assert "acknowledge-billing" in rejected.output
    assert fake_client.calls == []

    accepted = runner.invoke(
        main.app,
        [
            "--json",
            "grid",
            "queue",
            "--project",
            "project-1",
            "grid-1",
            "--plan-version",
            "1",
            "--plan-digest",
            "sha256:x",
            "--idempotency-key",
            "queue-1",
            "--acknowledge-billing",
        ],
    )
    assert accepted.exit_code == 0, accepted.output
    assert (
        fake_client.calls[-1][3]["billing_acknowledgement"]
        == "I understand FAL may bill this run even if checkpoint fetch fails"
    )


def test_eval_help_has_no_removed_run_command() -> None:
    result = runner.invoke(main.app, ["eval", "--help"])
    assert result.exit_code == 0, result.output
    assert "create" in result.output
    assert "run" not in result.output


def test_storage_single_copy_commands_are_discoverable() -> None:
    result = runner.invoke(main.app, ["storage", "single-copy", "--help"])

    assert result.exit_code == 0, result.output
    assert "audit" in result.output
    assert "ensure-remote" in result.output
    assert "evict-local" in result.output


def test_database_only_backup_and_restore_flags_are_discoverable() -> None:
    create = runner.invoke(main.app, ["backup", "create", "--help"])
    restore = runner.invoke(main.app, ["backup", "restore", "--help"])

    assert create.exit_code == 0, create.output
    assert restore.exit_code == 0, restore.output
    assert "--database-only" in create.output
    assert "--database-only" in restore.output
