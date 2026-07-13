from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx


class LLMConfigurationError(ValueError):
    pass


class LLMResponseError(ValueError):
    pass


_PROVIDER_ALIASES = {
    "openai": "openai",
    "openai-compatible": "openai-compatible",
    "openai_compatible": "openai-compatible",
    "openrouter": "openrouter",
    "open-router": "openrouter",
    "open_router": "openrouter",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "gemini": "gemini",
    "google": "gemini",
}


class PromptGenerator:
    def __init__(self, transport: httpx.BaseTransport | None = None, *, timeout: float = 60.0):
        self.transport = transport
        self.timeout = timeout

    def generate(
        self,
        *,
        provider: str,
        model: str,
        instruction: str,
        count: int,
        context: dict[str, Any] | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        normalized_provider = _PROVIDER_ALIASES.get(provider.strip().lower())
        if not normalized_provider:
            raise LLMConfigurationError(f"unsupported LLM provider: {provider}")
        if not model.strip():
            raise LLMConfigurationError("an LLM model is required")
        prompt = self._instruction(instruction, count, context or {})
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                if normalized_provider in {"openai", "openai-compatible"}:
                    raw = self._openai(client, normalized_provider, model, prompt, base_url)
                elif normalized_provider == "openrouter":
                    raw = self._openrouter(client, model, prompt, base_url)
                elif normalized_provider == "anthropic":
                    raw = self._anthropic(client, model, prompt, base_url)
                else:
                    raw = self._gemini(client, model, prompt, base_url)
        except httpx.HTTPError as exc:
            raise LLMResponseError("LLM provider request failed") from exc
        return {
            "provider": normalized_provider,
            "model": model,
            "prompts": self.normalize(raw, count=count),
        }

    @staticmethod
    def _instruction(instruction: str, count: int, context: dict[str, Any]) -> str:
        return (
            "Create an ordered image-model evaluation prompt set. Return JSON only, with this shape: "
            '{"prompts":[{"id":"short-slug","prompt":"complete prompt"}]}. '
            f"Return at most {count} prompts. Prompt IDs must be concise and unique. "
            "Do not include markdown fences or commentary.\n\n"
            f"Operator instruction:\n{instruction.strip()}\n\n"
            f"Structured context:\n{json.dumps(context, ensure_ascii=False, sort_keys=True)}"
        )

    @staticmethod
    def _key(*names: str) -> str:
        value = next((os.getenv(name) for name in names if os.getenv(name)), None)
        if not value:
            raise LLMConfigurationError(f"missing environment credential: {' or '.join(names)}")
        return value

    def _openai(self, client: httpx.Client, provider: str, model: str, prompt: str, base_url: str | None) -> str:
        key = self._key("LLM_API_KEY", "OPENAI_API_KEY")
        if provider == "openai-compatible" and not base_url:
            raise LLMConfigurationError("llm.base_url is required for openai-compatible")
        root = (base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{root}/chat/completions" if not root.endswith("/chat/completions") else root
        response = client.post(url, headers={"authorization": f"Bearer {key}"}, json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
        })
        self._raise(response)
        try:
            return str(response.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMResponseError("OpenAI response did not contain message content") from exc

    def _openrouter(self, client: httpx.Client, model: str, prompt: str, base_url: str | None) -> str:
        key = self._key("OPENROUTER_API_KEY", "LLM_API_KEY")
        root = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        url = f"{root}/chat/completions" if not root.endswith("/chat/completions") else root
        headers = {"authorization": f"Bearer {key}"}
        site_url = os.getenv("OPENROUTER_SITE_URL")
        site_title = os.getenv("OPENROUTER_SITE_TITLE")
        if site_url and site_url.strip():
            headers["HTTP-Referer"] = site_url.strip()
        if site_title and site_title.strip():
            headers["X-Title"] = site_title.strip()
        response = client.post(url, headers=headers, json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
        })
        self._raise(response)
        try:
            return str(response.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMResponseError("OpenRouter response did not contain message content") from exc

    def _anthropic(self, client: httpx.Client, model: str, prompt: str, base_url: str | None) -> str:
        key = self._key("LLM_API_KEY", "ANTHROPIC_API_KEY")
        root = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        url = f"{root}/messages" if not root.endswith("/messages") else root
        response = client.post(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, json={
            "model": model, "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        })
        self._raise(response)
        try:
            blocks = response.json()["content"]
            return "\n".join(str(block["text"]) for block in blocks if block.get("type") == "text")
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMResponseError("Anthropic response did not contain text content") from exc

    def _gemini(self, client: httpx.Client, model: str, prompt: str, base_url: str | None) -> str:
        key = self._key("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        root = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        url = f"{root}/models/{model}:generateContent"
        response = client.post(url, headers={"x-goog-api-key": key}, json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"},
        })
        self._raise(response)
        try:
            parts = response.json()["candidates"][0]["content"]["parts"]
            return "\n".join(str(part["text"]) for part in parts if "text" in part)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMResponseError("Gemini response did not contain text content") from exc

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_error:
            raise LLMResponseError(f"LLM provider returned HTTP {response.status_code}")

    @staticmethod
    def normalize(raw: str, *, count: int) -> list[dict[str, Any]]:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError("LLM response was not valid JSON") from exc
        items = decoded.get("prompts") if isinstance(decoded, dict) else decoded
        if not isinstance(items, list) or not items:
            raise LLMResponseError("LLM response requires a non-empty prompts array")
        result: list[dict[str, Any]] = []
        used: set[str] = set()
        for position, item in enumerate(items[:count]):
            if isinstance(item, str):
                prompt, requested_id, metadata = item, "", {}
            elif isinstance(item, dict):
                prompt = item.get("prompt") or item.get("text")
                requested_id = str(item.get("id") or "")
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            else:
                raise LLMResponseError(f"prompt at position {position} must be a string or object")
            if not isinstance(prompt, str) or not prompt.strip():
                raise LLMResponseError(f"prompt at position {position} is empty")
            if len(prompt) > 8000:
                raise LLMResponseError(f"prompt at position {position} exceeds 8000 characters")
            slug = re.sub(r"[^a-z0-9]+", "-", requested_id.lower()).strip("-")[:64]
            if not slug:
                slug = f"prompt-{position + 1:02d}"
            base, suffix = slug, 2
            while slug in used:
                slug, suffix = f"{base[:58]}-{suffix}", suffix + 1
            used.add(slug)
            result.append({"id": slug, "prompt": prompt.strip(), "position": position, "metadata": metadata})
        return result
