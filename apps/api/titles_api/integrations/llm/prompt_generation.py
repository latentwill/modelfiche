from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

import httpx

from .credentials import resolve_llm_credential


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
    def caption(
        self,
        *,
        provider: str,
        model: str,
        prompt: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        base_url: str | None = None,
    ) -> str:
        """Generate one caption from local image bytes using a multimodal chat API."""
        normalized_provider = _PROVIDER_ALIASES.get(provider.strip().lower())
        if normalized_provider not in {"openai", "openai-compatible", "openrouter", "anthropic", "gemini"}:
            raise LLMConfigurationError(f"unsupported caption provider: {provider}")
        if not model.strip():
            raise LLMConfigurationError("an LLM model is required")
        if not prompt.strip():
            raise LLMConfigurationError("a caption prompt is required")
        if not image_bytes:
            raise LLMConfigurationError("image bytes are required")
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                if normalized_provider in {"openai", "openai-compatible", "openrouter"}:
                    raw = self._openai_caption(client, normalized_provider, model, prompt, image_bytes, mime_type, base_url)
                elif normalized_provider == "anthropic":
                    raw = self._anthropic_caption(client, model, prompt, image_bytes, mime_type, base_url)
                else:
                    raw = self._gemini_caption(client, model, prompt, image_bytes, mime_type, base_url)
        except httpx.HTTPError as exc:
            raise LLMResponseError("LLM provider request failed") from exc
        return raw.strip()


    def list_models(
        self,
        *,
        provider: str,
        base_url: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch the provider's live model catalog for configuration pickers."""
        normalized = _PROVIDER_ALIASES.get(provider.strip().lower())
        if normalized not in {"openai", "openai-compatible", "openrouter", "anthropic", "gemini"}:
            raise LLMConfigurationError(f"unsupported LLM provider: {provider}")
        if normalized == "openai-compatible" and not base_url:
            raise LLMConfigurationError("llm.base_url is required for openai-compatible")

        if normalized == "openrouter":
            credential = resolve_llm_credential("openrouter")
            root = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
            headers = {"authorization": f"Bearer {credential.key}"} if credential else {}
        elif normalized in {"openai", "openai-compatible"}:
            key = self._key("LLM_API_KEY", "OPENAI_API_KEY")
            root = (base_url or "https://api.openai.com/v1").rstrip("/")
            headers = {"authorization": f"Bearer {key}"}
        elif normalized == "anthropic":
            key = self._key("LLM_API_KEY", "ANTHROPIC_API_KEY")
            root = (base_url or "https://api.anthropic.com/v1").rstrip("/")
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        else:
            key = self._key("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
            root = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
            headers = {"x-goog-api-key": key}

        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                response = client.get(f"{root}/models", headers=headers)
                self._raise(response)
                payload = response.json()
        except httpx.HTTPError as exc:
            raise LLMResponseError("Could not reach the LLM provider model catalog") from exc
        except ValueError as exc:
            raise LLMResponseError("LLM provider model catalog was not valid JSON") from exc

        raw_models = payload.get("data") if isinstance(payload, dict) else None
        if normalized == "gemini" and isinstance(payload, dict):
            raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise LLMResponseError("LLM provider model catalog did not contain models")

        models: list[dict[str, Any]] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            model_id = str(raw.get("id") or raw.get("name") or "").removeprefix("models/").strip()
            if not model_id:
                continue
            architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
            modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
            supports_images = "image" in modalities if isinstance(modalities, list) else None
            if normalized == "gemini":
                methods = raw.get("supportedGenerationMethods")
                if isinstance(methods, list) and "generateContent" not in methods:
                    continue
            label = str(raw.get("name") or raw.get("displayName") or model_id)
            if label.startswith("models/"):
                label = str(raw.get("displayName") or model_id)
            description_parts: list[str] = []
            if supports_images is True:
                description_parts.append("Vision")
            context_length = raw.get("context_length")
            if isinstance(context_length, (int, float)) and not isinstance(context_length, bool):
                description_parts.append(f"{int(context_length):,} token context")
            models.append({
                "id": model_id,
                "label": label,
                "description": " · ".join(description_parts),
                "supports_images": supports_images,
            })
        return sorted(models, key=lambda item: (str(item["label"]).lower(), str(item["id"]).lower()))

    def validate_model(
        self,
        *,
        provider: str,
        model: str,
        base_url: str | None = None,
    ) -> list[dict[str, Any]]:
        """Authenticate with the provider and return the catalog containing the selected model."""
        normalized = _PROVIDER_ALIASES.get(provider.strip().lower())
        if normalized == "openrouter":
            key = self._key("OPENROUTER_API_KEY", "LLM_API_KEY")
            root = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
            try:
                with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                    response = client.get(f"{root}/auth/key", headers={"authorization": f"Bearer {key}"})
                    self._raise(response)
            except httpx.HTTPError as exc:
                raise LLMResponseError("Could not reach OpenRouter credential validation") from exc
        models = self.list_models(provider=provider, base_url=base_url)
        if model not in {str(item["id"]) for item in models}:
            raise LLMConfigurationError(f"Model {model} was not found in the selected provider catalog")
        return models

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
        if value:
            return value
        credential = resolve_llm_credential("__saved__")
        if credential:
            return credential.key
        raise LLMConfigurationError(f"missing environment credential: {' or '.join(names)}")

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

    def _openai_caption(
        self,
        client: httpx.Client,
        provider: str,
        model: str,
        prompt: str,
        image_bytes: bytes,
        mime_type: str,
        base_url: str | None,
    ) -> str:
        key = self._key("OPENROUTER_API_KEY", "LLM_API_KEY") if provider == "openrouter" else self._key("LLM_API_KEY", "OPENAI_API_KEY")
        if provider == "openai-compatible" and not base_url:
            raise LLMConfigurationError("llm.base_url is required for openai-compatible")
        root = (base_url or ("https://openrouter.ai/api/v1" if provider == "openrouter" else "https://api.openai.com/v1")).rstrip("/")
        url = f"{root}/chat/completions" if not root.endswith("/chat/completions") else root
        data_url = f"data:{mime_type or 'application/octet-stream'};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        response = client.post(url, headers={"authorization": f"Bearer {key}"}, json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt.strip()},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            "temperature": 0.2,
        })
        self._raise(response)
        try:
            content = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            return content
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMResponseError("LLM response did not contain caption content") from exc

    def _anthropic_caption(
        self,
        client: httpx.Client,
        model: str,
        prompt: str,
        image_bytes: bytes,
        mime_type: str,
        base_url: str | None,
    ) -> str:
        key = self._key("LLM_API_KEY", "ANTHROPIC_API_KEY")
        root = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        url = f"{root}/messages" if not root.endswith("/messages") else root
        response = client.post(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, json={
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt.strip()},
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": mime_type or "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
            ]}],
        })
        self._raise(response)
        try:
            blocks = response.json()["content"]
            content = "".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict) and block.get("type") == "text")
            if not content.strip():
                raise ValueError
            return content
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMResponseError("Anthropic response did not contain caption content") from exc

    def _gemini_caption(
        self,
        client: httpx.Client,
        model: str,
        prompt: str,
        image_bytes: bytes,
        mime_type: str,
        base_url: str | None,
    ) -> str:
        key = self._key("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        root = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        url = f"{root}/models/{model}:generateContent"
        response = client.post(url, headers={"x-goog-api-key": key}, json={
            "contents": [{"role": "user", "parts": [
                {"text": prompt.strip()},
                {"inline_data": {"mime_type": mime_type or "image/jpeg", "data": base64.b64encode(image_bytes).decode("ascii")}},
            ]}],
            "generationConfig": {"temperature": 0.2},
        })
        self._raise(response)
        try:
            parts = response.json()["candidates"][0]["content"]["parts"]
            content = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict) and "text" in part)
            if not content.strip():
                raise ValueError
            return content
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMResponseError("Gemini response did not contain caption content") from exc

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
