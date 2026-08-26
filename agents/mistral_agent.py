"""Mistral AI OpenAI-compatible chat-completions provider.

Provider id remains ``mistral`` (not openai). API key via SecretStore only.
Trusted base URL only — no agent/request override.
"""

from __future__ import annotations

import os

import httpx

from agents.mistral_errors import (
    MISTRAL_DISABLED,
    MISTRAL_ENDPOINT_DENIED,
    MISTRAL_MALFORMED_RESPONSE,
    MISTRAL_MISSING_KEY,
    MISTRAL_MISSING_MODEL,
    MISTRAL_TIMEOUT,
    MistralProviderError,
    error_from_http_status,
)
from agents.mistral_versions import (
    MISTRAL_DEFAULT_BASE_URL,
    MISTRAL_PROVIDER_ADAPTER_VERSION,
    MISTRAL_PROVIDER_ID,
    MISTRAL_TRUSTED_BASE_URLS,
    resolve_mistral_chat_temperature,
)
from agents.provider_result import usage_from_chat_completions_response
from security.secrets import EnvSecretStore, SecretStore


def mistral_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("MISTRAL_ENABLED", "false")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def resolve_mistral_model(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    return str(
        source.get("MISTRAL_DEFAULT_MODEL")
        or source.get("MISTRAL_MODEL")
        or ""
    ).strip()


def resolve_mistral_base_url(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    raw = str(source.get("MISTRAL_BASE_URL") or MISTRAL_DEFAULT_BASE_URL).strip().rstrip("/")
    if raw not in MISTRAL_TRUSTED_BASE_URLS:
        raise MistralProviderError(MISTRAL_ENDPOINT_DENIED)
    return raw


def resolve_mistral_timeout(env: dict | None = None) -> float:
    source = env if env is not None else os.environ
    try:
        return float(source.get("MISTRAL_TIMEOUT_SECONDS", "120") or 120)
    except (TypeError, ValueError):
        return 120.0


def mistral_status(*, enabled: bool, has_key: bool, has_model: bool) -> str:
    """disabled | ready | blocked | degraded."""

    if not enabled:
        return "disabled"
    if not has_key or not has_model:
        return "blocked"
    return "ready"


class MistralAgent:
    """Production Mistral adapter. Does not invoke ToolGateway."""

    provider_id = MISTRAL_PROVIDER_ID
    adapter_version = MISTRAL_PROVIDER_ADAPTER_VERSION

    def __init__(
        self,
        *,
        secret_store: SecretStore | None = None,
        env: dict | None = None,
        transport=None,
    ):
        source = env if env is not None else os.environ
        if not mistral_enabled(source):
            raise MistralProviderError(MISTRAL_DISABLED)
        store = secret_store or EnvSecretStore()
        # Never assign raw key to a public attribute used by repr/logs.
        api_key = store.get("MISTRAL_API_KEY")
        if not api_key:
            raise MistralProviderError(MISTRAL_MISSING_KEY)
        self._api_key = api_key
        self.model = resolve_mistral_model(source)
        if not self.model:
            raise MistralProviderError(MISTRAL_MISSING_MODEL)
        self.base_url = resolve_mistral_base_url(source)
        self.timeout_seconds = resolve_mistral_timeout(source)
        self._temperature = resolve_mistral_chat_temperature(source)
        self._transport = transport
        self.tool_invocations = 0

    def __repr__(self) -> str:
        return (
            f"MistralAgent(provider_id={self.provider_id!r}, model={self.model!r}, "
            f"base_url={self.base_url!r})"
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def run(self, prompt: str):
        """Canonical prompt → Mistral chat completions → ProviderResult."""

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": str(prompt or "")}],
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        url = f"{self.base_url}/chat/completions"
        try:
            if self._transport is not None:
                response = await self._transport.post(
                    url, headers=self._headers(), json=payload, timeout=self.timeout_seconds
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise MistralProviderError(MISTRAL_TIMEOUT, retryable=True) from exc
        except MistralProviderError:
            raise
        except Exception as exc:
            raise MistralProviderError(MISTRAL_MALFORMED_RESPONSE) from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise error_from_http_status(status)

        try:
            data = response.json()
        except Exception as exc:
            raise MistralProviderError(MISTRAL_MALFORMED_RESPONSE) from exc

        if not isinstance(data, dict):
            raise MistralProviderError(MISTRAL_MALFORMED_RESPONSE)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise MistralProviderError(MISTRAL_MALFORMED_RESPONSE)
        first = choices[0] if isinstance(choices[0], dict) else None
        if first is None:
            raise MistralProviderError(MISTRAL_MALFORMED_RESPONSE)
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = message.get("content")
        if text is None:
            raise MistralProviderError(MISTRAL_MALFORMED_RESPONSE)

        # Tool intent text must never trigger ToolGateway from this adapter.
        return usage_from_chat_completions_response(
            data,
            provider_id=MISTRAL_PROVIDER_ID,
            model_id=self.model,
            text=str(text),
        )
