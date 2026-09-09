import json

import httpx
import pytest
from fastapi.testclient import TestClient

from titles_api.integrations.llm import LLMConfigurationError, PromptGenerator
from titles_api.routers import operator_config


@pytest.mark.parametrize(
    ("provider", "model", "base_url", "credential", "response_body", "expected_path"),
    [
        ("openai-compatible", "local-model", "https://llm.example/v1", ("LLM_API_KEY", "openai-secret"), {"choices": [{"message": {"content": '{"prompts":[{"id":"Front View","prompt":"token, front view"}]}'}}]}, "/v1/chat/completions"),
        ("openrouter", "openrouter/auto", None, ("OPENROUTER_API_KEY", "openrouter-secret"), {"choices": [{"message": {"content": '{"prompts":[{"id":"router","prompt":"token, routed model"}]}'}}]}, "/api/v1/chat/completions"),
        ("anthropic", "claude-example", None, ("ANTHROPIC_API_KEY", "anthropic-secret"), {"content": [{"type": "text", "text": '{"prompts":[{"id":"profile","prompt":"token, side profile"}]}'}]}, "/v1/messages"),
        ("gemini", "gemini-example", None, ("GEMINI_API_KEY", "gemini-secret"), {"candidates": [{"content": {"parts": [{"text": '{"prompts":[{"id":"wide","prompt":"token, wide crop"}]}' }]}}]}, "/v1beta/models/gemini-example:generateContent"),
    ],
)
def test_provider_transports_normalize_without_real_network(monkeypatch, provider, model, base_url, credential, response_body, expected_path):
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_SITE_URL", "OPENROUTER_SITE_TITLE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(*credential)
    requests = []

    def respond(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json=response_body)

    result = PromptGenerator(httpx.MockTransport(respond)).generate(
        provider=provider, model=model, base_url=base_url,
        instruction="Vary viewpoint", count=4, context={"trigger_words": ["token"]},
    )
    assert result["provider"] == provider
    assert result["prompts"][0]["position"] == 0
    assert result["prompts"][0]["id"] in {"front-view", "router", "profile", "wide"}
    assert requests[0].url.path == expected_path
    assert credential[1] not in str(requests[0].url)


def test_openrouter_headers_override_and_generic_key_fallback(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "generic-secret")
    monkeypatch.setenv("OPENROUTER_SITE_URL", "https://titles.example")
    monkeypatch.setenv("OPENROUTER_SITE_TITLE", "Titles DAM")
    requests = []

    def respond(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"prompts":["router prompt"]}'}}]})

    result = PromptGenerator(httpx.MockTransport(respond)).generate(
        provider="open_router",
        model="router/model",
        base_url="https://router.example/v1",
        instruction="Keep it concise",
        count=1,
    )
    assert result["provider"] == "openrouter"
    assert result["prompts"][0]["id"] == "prompt-01"
    assert str(requests[0].url) == "https://router.example/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer generic-secret"
    assert requests[0].headers["HTTP-Referer"] == "https://titles.example"
    assert requests[0].headers["X-Title"] == "Titles DAM"


def test_openrouter_caption_uses_selected_model_and_image_payload(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-caption-secret")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "A red square."}}]})

    caption = PromptGenerator(httpx.MockTransport(respond)).caption(
        provider="openrouter",
        model="openai/gpt-4.1-mini",
        prompt="Caption only visible content.",
        image_bytes=b"png-bytes",
        mime_type="image/png",
    )
    assert caption == "A red square."
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer router-caption-secret"
    payload = json.loads(request.content)
    assert payload["model"] == "openai/gpt-4.1-mini"
    assert payload["messages"][0]["content"][0] == {"type": "text", "text": "Caption only visible content."}
    assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openrouter_requires_openrouter_or_generic_credential(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr("titles_api.integrations.llm.prompt_generation.resolve_llm_credential", lambda _provider: None)
    with pytest.raises(LLMConfigurationError, match="OPENROUTER_API_KEY or LLM_API_KEY"):
        PromptGenerator(httpx.MockTransport(lambda _request: httpx.Response(500))).generate(
            provider="openrouter",
            model="router/model",
            instruction="Keep it concise",
            count=1,
        )




def test_openrouter_saved_config_catalog_and_connection_check(client: TestClient, monkeypatch):
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    saved = client.patch(
        "/api/operator-settings",
        json={"llm": {"provider": "openrouter", "model": "openai/gpt-4.1-mini"}},
    )
    assert saved.status_code == 200
    assert saved.json()["llm"] == {"provider": "openrouter", "model": "openai/gpt-4.1-mini", "base_url": None}
    assert saved.json()["credentials"]["llm_configured"] is False
    missing = client.post("/api/operator-settings/test/llm")
    assert missing.status_code == 409

    monkeypatch.setenv("OPENROUTER_API_KEY", "router-secret")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={"data": [
            {
                "id": "text/only",
                "name": "Text only",
                "architecture": {"input_modalities": ["text"]},
            },
            {
                "id": "openai/gpt-4.1-mini",
                "name": "GPT-4.1 Mini",
                "context_length": 1048576,
                "architecture": {"input_modalities": ["text", "image"]},
            },
        ]})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(operator_config, "PromptGenerator", lambda: PromptGenerator(transport))
    catalog = client.get(
        "/api/operator-settings/llm-models",
        params={"provider": "openrouter", "capability": "image"},
    )
    assert catalog.status_code == 200
    assert catalog.json()["models"] == [{
        "id": "openai/gpt-4.1-mini",
        "label": "GPT-4.1 Mini",
        "description": "Vision · 1,048,576 token context",
        "supports_images": True,
    }]
    assert catalog.json()["credential"] == {"configured": True, "source": "OPENROUTER_API_KEY"}
    checked = client.post(
        "/api/operator-settings/test/llm",
        json={"provider": "openrouter", "model": "openai/gpt-4.1-mini", "base_url": None},
    )
    assert checked.status_code == 200
    assert checked.json()["catalog_size"] == 2
    assert checked.json()["mode"] == "network_validated"
    assert checked.json()["network_called"] is True
    assert len(requests) == 3
    assert [request.url.path for request in requests] == ["/api/v1/models", "/api/v1/auth/key", "/api/v1/models"]
    assert all(request.headers["Authorization"] == "Bearer router-secret" for request in requests)


def test_caption_presets_are_persisted_and_validated(client: TestClient):
    saved = client.patch(
        "/api/operator-settings",
        json={"captioning": {"presets": [{"name": "Visible scene", "prompt": "Describe only visible subjects and objects."}]}},
    )
    assert saved.status_code == 200
    assert saved.json()["captioning"]["presets"] == [{"name": "Visible scene", "prompt": "Describe only visible subjects and objects."}]
    duplicate = client.patch(
        "/api/operator-settings",
        json={"captioning": {"presets": [{"name": "Visible scene", "prompt": "One."}, {"name": "visible scene", "prompt": "Two."}]}},
    )
    assert duplicate.status_code == 422
    assert "unique" in duplicate.json()["detail"]
def test_prompt_set_generate_api_is_explicit_preview_contract(client: TestClient, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-local-key")

    def respond(request: httpx.Request):
        submitted = json.loads(request.content)
        assert "ordered image-model evaluation prompt set" in submitted["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"prompts": [
            {"id": "direct", "prompt": "token portrait, direct gaze"},
            {"id": "direct", "prompt": "token portrait, three-quarter view"},
        ]})}}]})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(operator_config, "PromptGenerator", lambda: PromptGenerator(transport))
    settings = client.patch("/api/operator-settings", json={"llm": {"provider": "openai", "model": "test-model"}})
    assert settings.status_code == 200
    response = client.post("/api/prompt-sets/generate", json={
        "instruction": "Create identity holdouts", "count": 6,
        "context": {"trigger_words": ["token"], "project_id": "local-preview"},
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["persisted"] is False
    assert [item["position"] for item in payload["prompts"]] == [0, 1]
    assert [item["id"] for item in payload["prompts"]] == ["direct", "direct-2"]

    operation = client.app.openapi()["paths"]["/api/prompt-sets/generate"]["post"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("PromptSetGenerateResponse")


def test_saved_llm_key_is_write_only_fallback_and_env_wins(client: TestClient, monkeypatch):
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    saved = client.put("/api/operator-settings/llm-key", json={"key": "saved-secret"})
    assert saved.status_code == 200
    assert saved.json()["configured"] is True
    assert "key" not in saved.json()
    settings = client.patch("/api/operator-settings", json={"llm": {"provider": "openai", "model": "vision"}})
    assert settings.json()["credentials"]["llm_configured"] is True
    assert settings.json()["credentials"]["llm_source"] == "saved"
    requests = []

    def respond(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"prompts":["ok"]}'}}]})

    PromptGenerator(httpx.MockTransport(respond)).generate(
        provider="openai", model="text", instruction="x", count=1
    )
    assert requests[0].headers["Authorization"] == "Bearer saved-secret"
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    status_response = client.get("/api/operator-settings")
    assert status_response.json()["credentials"]["llm_source"] == "OPENAI_API_KEY"
    PromptGenerator(httpx.MockTransport(respond)).generate(
        provider="openai", model="text", instruction="x", count=1
    )
    assert requests[-1].headers["Authorization"] == "Bearer environment-secret"
    assert client.delete("/api/operator-settings/llm-key").json()["configured"] is True
