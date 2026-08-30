"""Bounded HTTP client for production provider adapters."""

from __future__ import annotations

import time
from typing import Any, Mapping

import httpx

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory, classify_http_status


class BoundedHttpClient:
    def __init__(
        self,
        *,
        provider_id: str,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 1_048_576,
    ):
        self.provider_id = provider_id
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
            )
        return self._client

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        started = time.monotonic()
        try:
            resp = self._get_client().request(
                method.upper(),
                url,
                headers=dict(headers or {}),
                json=json_body,
                content=content,
            )
        except httpx.TimeoutException as exc:
            raise ProductionProviderError(
                ProviderErrorCategory.TIMEOUT,
                message="request_timeout",
                provider_id=self.provider_id,
                retryable=True,
                metadata={"latency_ms": round((time.monotonic() - started) * 1000, 2)},
            ) from exc
        except httpx.RequestError as exc:
            raise ProductionProviderError(
                ProviderErrorCategory.NETWORK_ERROR,
                message=str(type(exc).__name__),
                provider_id=self.provider_id,
                retryable=True,
            ) from exc
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else None
            raise ProductionProviderError(
                ProviderErrorCategory.RATE_LIMITED,
                message="rate_limited",
                provider_id=self.provider_id,
                retryable=True,
                retry_after_seconds=delay,
            )
        if resp.status_code >= 400:
            cat = classify_http_status(resp.status_code)
            raise ProductionProviderError(
                cat,
                message=f"http_{resp.status_code}",
                provider_id=self.provider_id,
                retryable=cat in {ProviderErrorCategory.RATE_LIMITED, ProviderErrorCategory.TIMEOUT, ProviderErrorCategory.PROVIDER_UNAVAILABLE},
            )
        if len(resp.content) > self.max_response_bytes:
            raise ProductionProviderError(
                ProviderErrorCategory.INVALID_RESPONSE,
                message="response_too_large",
                provider_id=self.provider_id,
            )
        return resp

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
