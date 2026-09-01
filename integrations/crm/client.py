"""Bounded CRM HTTP client."""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse

from integrations.crm.config import CrmIntegrationConfig
from integrations.crm.errors import CrmIntegrationError
from integrations.production.http import BoundedHttpClient


class CrmHttpClient:
    ALLOWED_HOSTS = frozenset({"api.hubspot.com", "api.salesforce.com", "api.pipedrive.com"})

    def __init__(self, *, config: CrmIntegrationConfig, secret_resolver: Callable[[str], str | None] | None = None):
        self._config = config
        self._secret_resolver = secret_resolver
        self._http = BoundedHttpClient(provider_id="crm", timeout_seconds=config.timeout_seconds)

    def get(self, path: str, *, credential_ref: str = "") -> dict:
        if not self._config.is_live:
            raise CrmIntegrationError("INTEGRATION_ENVIRONMENT_MISMATCH")
        url = self._config.api_base_url.rstrip("/") + path
        host = (urlparse(url).hostname or "").lower()
        if host not in self.ALLOWED_HOSTS:
            raise CrmIntegrationError("ssrf_host_not_allowed")
        key = self._config._resolved_key()
        if not key:
            raise CrmIntegrationError("INTEGRATION_NOT_CONFIGURED")
        resp = self._http.request("GET", url, headers={"Authorization": f"Bearer {key}"})
        import json

        body = resp.content[: self._http.max_response_bytes].decode("utf-8", errors="replace")
        return json.loads(body) if body else {}
