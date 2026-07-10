import json

import httpx
import pytest
from fastapi.testclient import TestClient

from titles_api.integrations.llm import PromptGenerator
from titles_api.routers import operator_config


@pytest.mark.parametrize(
    ("provider", "model", "base_url", "credential", "response_body", "expected_path"),
    [
        ("openai-compatible", "local-model", "https://llm.example/v1", ("LLM_API_KEY", "openai-secret"), {"choices": [{"message": {"content": '{"prompts":[{"id":"Front View","prompt":"token, front view"}]}'}}]}, "/v1/chat/completions"),
        ("anthropic", "claude-example", None, ("ANTHROPIC_API_KEY", "anthropic-secret"), {"content": [{"type": "text", "text": '{"prompts":[{"id":"profile","prompt":"token, side profile"}]}'}]}, "/v1/messages"),
        ("gemini", "gemini-example", None, ("GEMINI_API_KEY", "gemini-secret"), {"candidates": [{"content": {"parts": [{"text": '{"prompts":[{"id":"wide","prompt":"token, wide crop"}]}' }]}}]}, "/v1beta/models/gemini-example:generateContent"),
    ],
)
def test_provider_transports_normalize_without_real_network(monkeypatch, provider, model, base_url, credential, response_body, expected_path):
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
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
    assert result["prompts"][0]["id"] in {"front-view", "profile", "wide"}
    assert requests[0].url.path == expected_path
    assert credential[1] not in str(requests[0].url)


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
