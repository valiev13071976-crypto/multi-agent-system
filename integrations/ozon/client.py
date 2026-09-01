"""Bounded Ozon HTTP client — governed endpoints only."""

from __future__ import annotations

import json
from typing import Callable
from urllib.parse import urlparse

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient
from integrations.ozon.config import OzonIntegrationConfig
from integrations.ozon.errors import OzonIntegrationError


class OzonHttpClient:
    ALLOWED_HOSTS = frozenset({"api-seller.ozon.ru", "api.ozon.ru"})

    def __init__(self, *, config: OzonIntegrationConfig, secret_resolver: Callable[[str], str | None] | None = None):
        self._config = config
        self._secret_resolver = secret_resolver
        self._http = BoundedHttpClient(provider_id="ozon", timeout_seconds=config.timeout_seconds)

    def _assert_host(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if host not in self.ALLOWED_HOSTS:
            raise OzonIntegrationError("ssrf_host_not_allowed")

    def _client_id(self, credential_ref: str = "") -> str:
        if self._secret_resolver and credential_ref:
            val = str(self._secret_resolver(credential_ref) or "").strip()
            if val and ":" in val:
                return val.split(":", 1)[0]
        return self._config._resolved_client_id()

    def _api_key(self, credential_ref: str = "") -> str:
        if self._secret_resolver and credential_ref:
            val = str(self._secret_resolver(credential_ref) or "").strip()
            if val and ":" in val:
                return val.split(":", 1)[1]
            if val:
                return val
        return self._config._resolved_api_key()

    def post(self, path: str, *, body: dict, credential_ref: str = "") -> dict:
        if not self._config.is_live:
            raise OzonIntegrationError("INTEGRATION_ENVIRONMENT_MISMATCH")
        url = self._config.api_base_url.rstrip("/") + path
        self._assert_host(url)
        client_id = self._client_id(credential_ref)
        api_key = self._api_key(credential_ref)
        if not client_id or not api_key:
            raise OzonIntegrationError("INTEGRATION_NOT_CONFIGURED")
        headers = {"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"}
        try:
            resp = self._http.request("POST", url, headers=headers, body=json.dumps(body).encode())
        except ProductionProviderError as exc:
            self._raise_normalized(exc)
        raw = resp.content[: self._http.max_response_bytes].decode("utf-8", errors="replace")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise OzonIntegrationError("INTEGRATION_MALFORMED_RESPONSE") from exc

    def _raise_normalized(self, exc: ProductionProviderError) -> None:
        if exc.category == ProviderErrorCategory.RATE_LIMITED:
            from integrations.activation.errors import IntegrationRateLimitedError

            raise IntegrationRateLimitedError() from exc
        if exc.category == ProviderErrorCategory.TIMEOUT:
            from integrations.activation.errors import IntegrationTimeoutNormalizedError

            raise IntegrationTimeoutNormalizedError() from exc
        if exc.category in {ProviderErrorCategory.AUTH_FAILED, ProviderErrorCategory.FORBIDDEN}:
            from integrations.activation.errors import IntegrationAuthFailedError

            raise IntegrationAuthFailedError() from exc
        raise OzonIntegrationError(str(exc.category.value)) from exc
