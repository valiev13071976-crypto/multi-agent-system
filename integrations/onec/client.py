"""Bounded 1C HTTP client — configured endpoint only."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from integrations.onec.config import OneCIntegrationConfig
from integrations.onec.errors import OneCIntegrationError
from integrations.onec.transport import OneCTransport
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient


class OneCHttpClient:
    def __init__(
        self,
        *,
        config: OneCIntegrationConfig,
        secret_resolver: Callable[[str], str | None] | None = None,
    ):
        self._config = config
        self._secret_resolver = secret_resolver
        base = config._resolved_base_url()
        self._transport = OneCTransport(base_url=base, transport=config.transport) if base else None
        self._http = BoundedHttpClient(provider_id="onec", timeout_seconds=config.timeout_seconds)

    def _auth_headers(self, credential_ref: str = "") -> dict[str, str]:
        if self._config.auth_mode == "bearer":
            token = ""
            if self._secret_resolver and credential_ref:
                token = str(self._secret_resolver(credential_ref) or "")
            if not token:
                token = self._config._resolved_token()
            if token:
                return {"Authorization": f"Bearer {token}"}
        return {}

    def call(
        self,
        path: str,
        *,
        credential_ref: str = "",
        params: Mapping[str, Any] | None = None,
        method: str = "GET",
    ) -> dict:
        if not self._config.is_live:
            raise OneCIntegrationError("INTEGRATION_ENVIRONMENT_MISMATCH")
        if self._transport is None or not self._transport.is_supported():
            raise OneCIntegrationError("INTEGRATION_NOT_CONFIGURED")
        url = self._transport.resolve_url(path)
        headers = self._auth_headers(credential_ref)
        try:
            resp = self._http.request(method, url, headers=headers, json_body=dict(params or {}) if method != "GET" else None)
        except ProductionProviderError as exc:
            if exc.category == ProviderErrorCategory.RATE_LIMITED:
                from integrations.activation.errors import IntegrationRateLimitedError

                raise IntegrationRateLimitedError() from exc
            if exc.category == ProviderErrorCategory.TIMEOUT:
                from integrations.activation.errors import IntegrationTimeoutNormalizedError

                raise IntegrationTimeoutNormalizedError() from exc
            if exc.category in {ProviderErrorCategory.AUTH_FAILED, ProviderErrorCategory.FORBIDDEN}:
                from integrations.activation.errors import IntegrationAuthFailedError

                raise IntegrationAuthFailedError() from exc
            raise OneCIntegrationError(str(exc.category.value)) from exc
        body = resp.content[: self._http.max_response_bytes].decode("utf-8", errors="replace")
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise OneCIntegrationError("INTEGRATION_MALFORMED_RESPONSE") from exc
