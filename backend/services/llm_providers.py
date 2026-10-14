"""
LLM providers behind one small interface, so the AI validation step does not
depend on any single vendor.

* ``OpenAICompatibleProvider`` covers every API that implements the OpenAI
  ``/chat/completions`` contract: OpenAI, Mistral, OpenRouter, Ollama, vLLM...
* ``GeminiProvider`` calls the Google Generative Language API.

Both use ``httpx`` only (no vendor SDK). Errors are mapped to two categories:
``ProviderAuthError`` (bad key, no credit: retrying is useless, alert someone)
and ``ProviderError`` with a ``retryable`` flag (rate limits, 5xx, network).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import httpx

from config import Settings


@dataclass
class LLMReply:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool, status_code: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class ProviderAuthError(ProviderError):
    """401/402/403: invalid key, missing permission or exhausted credit."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message, retryable=False, status_code=status_code)


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, prompt: str, max_tokens: int = 4096) -> LLMReply: ...


def _raise_for_status(response: httpx.Response, provider: str) -> None:
    code = response.status_code
    if code < 400:
        return
    message = f"{provider} returned HTTP {code}: {response.text[:300]}"
    if code in (401, 402, 403):
        raise ProviderAuthError(message, code)
    raise ProviderError(message, retryable=code == 429 or code >= 500, status_code=code)


def _post(client: httpx.Client, provider: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.post(url, **kwargs)
    except httpx.TransportError as exc:
        raise ProviderError(f"{provider} unreachable: {exc}", retryable=True) from exc
    _raise_for_status(response, provider)
    data: dict[str, Any] = response.json()
    return data


class OpenAICompatibleProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_s: float = 90.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.name = name
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.Client(timeout=timeout_s)

    def complete(self, prompt: str, max_tokens: int = 4096) -> LLMReply:
        data = _post(
            self._client, self.name, self._url, headers=self._headers,
            json={"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]},
        )
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return LLMReply(text, {
            "provider": self.name,
            "model": self.model,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        })


class GeminiProvider:
    name = "gemini"
    _BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_s: float = 90.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.model = model
        self._headers = {"x-goog-api-key": api_key}
        self._client = client or httpx.Client(timeout=timeout_s)

    def complete(self, prompt: str, max_tokens: int = 4096) -> LLMReply:
        data = _post(
            self._client, self.name, f"{self._BASE}/{self.model}:generateContent",
            headers=self._headers,
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens,
                                       "responseMimeType": "application/json"}},
        )
        candidates = data.get("candidates") or [{}]
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata") or {}
        return LLMReply(text, {
            "provider": self.name,
            "model": self.model,
            "input_tokens": usage.get("promptTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0),
        })


def build_providers(settings: Settings) -> list[LLMProvider]:
    """Providers in the order given by ``AI_PROVIDERS``; those without an API key are skipped."""
    providers: list[LLMProvider] = []
    for name in (n.strip().lower() for n in settings.AI_PROVIDERS.split(",")):
        if name == "gemini" and settings.GEMINI_API_KEY:
            providers.append(GeminiProvider(settings.GEMINI_API_KEY, settings.GEMINI_MODEL))
        elif name == "openai_compatible" and settings.OPENAI_COMPATIBLE_API_KEY:
            providers.append(OpenAICompatibleProvider(
                "openai_compatible", settings.OPENAI_COMPATIBLE_BASE_URL,
                settings.OPENAI_COMPATIBLE_API_KEY, settings.OPENAI_COMPATIBLE_MODEL,
            ))
        elif name == "openrouter" and settings.OPENROUTER_API_KEY:
            providers.append(OpenAICompatibleProvider(
                "openrouter", "https://openrouter.ai/api/v1",
                settings.OPENROUTER_API_KEY, settings.OPENROUTER_MODEL,
            ))
    return providers
