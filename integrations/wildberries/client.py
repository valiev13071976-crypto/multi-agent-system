"""Bounded Wildberries HTTP client — governed endpoints only."""

from __future__ import annotations

import json
from typing import Callable
from urllib.parse import urlparse

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient
from integrations.wildberries.config import WildberriesIntegrationConfig
from integrations.wildberries.errors import WildberriesIntegrationError


class WildberriesHttpClient:
    ALLOWED_HOSTS = frozenset({"suppliers-api.wildberries.ru", "content-api.wildberries.ru", "marketplace-api.wildberries.ru"})

    def __init__(self, *, config: WildberriesIntegrationConfig, secret_resolver: Callable[[str], str | None] | None = None):
        self._config = config
        self._secret_resolver = secret_resolver
        self._http = BoundedHttpClient(provider_id="wildberries", timeout_seconds=config.timeout_seconds)

    def _assert_host(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if host not in self.ALLOWED_HOSTS:
            raise WildberriesIntegrationError("ssrf_host_not_allowed")

    def _token(self, credential_ref: str = "") -> str:
        if self._secret_resolver and credential_ref:
            tok = str(self._secret_resolver(credential_ref) or "").strip()
            if tok:
                return tok
        return self._config._resolved_token()

    def get(self, path: str, *, credential_ref: str = "") -> dict:
        if not self._config.is_live:
            raise WildberriesIntegrationError("INTEGRATION_ENVIRONMENT_MISMATCH")
        url = self._config.api_base_url.rstrip("/") + path
        self._assert_host(url)
        token = self._token(credential_ref)
        if not token:
            raise WildberriesIntegrationError("INTEGRATION_NOT_CONFIGURED")
        try:
            resp = self._http.request("GET", url, headers={"Authorization": token})
        except ProductionProviderError as exc:
            self._raise_normalized(exc)
        body = resp.content[: self._http.max_response_bytes].decode("utf-8", errors="replace")
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise WildberriesIntegrationError("INTEGRATION_MALFORMED_RESPONSE") from exc

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
        raise WildberriesIntegrationError(str(exc.category.value)) from exc
