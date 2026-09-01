"""Bounded Calendar HTTP client."""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse

from integrations.calendar.config import CalendarIntegrationConfig
from integrations.calendar.errors import CalendarIntegrationError
from integrations.production.http import BoundedHttpClient


class CalendarHttpClient:
    ALLOWED_HOSTS = frozenset({"www.googleapis.com", "graph.microsoft.com"})

    def __init__(self, *, config: CalendarIntegrationConfig, secret_resolver: Callable[[str], str | None] | None = None):
        self._config = config
        self._secret_resolver = secret_resolver
        self._http = BoundedHttpClient(provider_id="calendar", timeout_seconds=config.timeout_seconds)

    def get(self, path: str, *, credential_ref: str = "") -> dict:
        if not self._config.is_live:
            raise CalendarIntegrationError("INTEGRATION_ENVIRONMENT_MISMATCH")
        url = self._config.api_base_url.rstrip("/") + path
        host = (urlparse(url).hostname or "").lower()
        if host not in self.ALLOWED_HOSTS:
            raise CalendarIntegrationError("ssrf_host_not_allowed")
        token = self._config._resolved_token()
        if not token:
            raise CalendarIntegrationError("INTEGRATION_NOT_CONFIGURED")
        resp = self._http.request("GET", url, headers={"Authorization": f"Bearer {token}"})
        import json

        body = resp.content[: self._http.max_response_bytes].decode("utf-8", errors="replace")
        return json.loads(body) if body else {}
