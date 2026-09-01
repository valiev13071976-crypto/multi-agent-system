"""Bounded Email HTTP client."""

from __future__ import annotations

import json
from typing import Callable
from urllib.parse import urlparse

from integrations.email.config import EmailIntegrationConfig
from integrations.email.errors import EmailIntegrationError
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient


class EmailHttpClient:
    ALLOWED_HOSTS = frozenset({"gmail.googleapis.com", "graph.microsoft.com", "api.sendgrid.com"})

    def __init__(self, *, config: EmailIntegrationConfig, secret_resolver: Callable[[str], str | None] | None = None):
        self._config = config
        self._secret_resolver = secret_resolver
        self._http = BoundedHttpClient(provider_id="email", timeout_seconds=config.timeout_seconds)

    def _assert_host(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if host not in self.ALLOWED_HOSTS:
            raise EmailIntegrationError("ssrf_host_not_allowed")

    def get(self, path: str, *, credential_ref: str = "") -> dict:
        if not self._config.is_live:
            raise EmailIntegrationError("INTEGRATION_ENVIRONMENT_MISMATCH")
        url = self._config.api_base_url.rstrip("/") + path
        self._assert_host(url)
        token = ""
        if self._secret_resolver and credential_ref:
            token = str(self._secret_resolver(credential_ref) or "").strip()
        if not token:
            token = self._config._resolved_token()
        if not token:
            raise EmailIntegrationError("INTEGRATION_NOT_CONFIGURED")
        try:
            resp = self._http.request("GET", url, headers={"Authorization": f"Bearer {token}"})
        except ProductionProviderError as exc:
            self._raise_normalized(exc)
        body = resp.content[: self._http.max_response_bytes].decode("utf-8", errors="replace")
        return json.loads(body) if body else {}

    def _raise_normalized(self, exc: ProductionProviderError) -> None:
        if exc.category == ProviderErrorCategory.RATE_LIMITED:
            from integrations.activation.errors import IntegrationRateLimitedError

            raise IntegrationRateLimitedError() from exc
        raise EmailIntegrationError(str(exc.category.value)) from exc
