"""Bounded Bitrix REST HTTP client — dormant without LIVE config."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

from integrations.bitrix.config import BitrixIntegrationConfig
from integrations.bitrix.errors import BitrixIntegrationError
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient


class BitrixHttpClient:
    """Production-capable Bitrix webhook client — no network unless explicitly configured."""

    def __init__(
        self,
        *,
        config: BitrixIntegrationConfig,
        secret_resolver: Callable[[str], str | None] | None = None,
    ):
        self._config = config
        self._secret_resolver = secret_resolver
        self._http = BoundedHttpClient(provider_id="bitrix", timeout_seconds=config.timeout_seconds)

    def _webhook_base(self, credential_ref: str = "") -> str:
        url = ""
        if self._secret_resolver and credential_ref:
            url = str(self._secret_resolver(credential_ref) or "").strip()
        if not url:
            url = self._config._resolved_webhook_url()
        if not url:
            raise BitrixIntegrationError("INTEGRATION_NOT_CONFIGURED")
        if "://" not in url:
            raise BitrixIntegrationError("INTEGRATION_VALIDATION_FAILED")
        return url.rstrip("/") + "/"

    def call(
        self,
        method: str,
        *,
        credential_ref: str = "",
        params: Mapping[str, Any] | None = None,
        idempotent: bool = True,
    ) -> dict:
        if not self._config.is_live:
            raise BitrixIntegrationError("INTEGRATION_ENVIRONMENT_MISMATCH")
        base = self._webhook_base(credential_ref)
        url = urljoin(base, f"{method}.json")
        try:
            resp = self._http.request("POST", url, json_body=dict(params or {}))
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
            raise BitrixIntegrationError(str(exc.category.value)) from exc

        body_text = resp.content[: self._http.max_response_bytes].decode("utf-8", errors="replace")
        try:
            data = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError as exc:
            raise BitrixIntegrationError("INTEGRATION_MALFORMED_RESPONSE") from exc
        if "error" in data:
            raise BitrixIntegrationError(str(data.get("error_description") or data.get("error")))
        return data

    def safe_call_metadata(self) -> dict:
        return self._config.safe_metadata()
