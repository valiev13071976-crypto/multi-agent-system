"""Bounded Bitrix REST HTTP client — dormant without LIVE config."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

from integrations.bitrix.config import BitrixIntegrationConfig
from integrations.bitrix.errors import BitrixIntegrationError
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient


def _describe_provider_error(exc: ProductionProviderError) -> str:
    """Best-effort, bounded, sanitized description of a failed Bitrix REST
    call. Production defect closure: a bare HTTP status category (e.g.
    "BAD_REQUEST") never says *why* Bitrix rejected a request -- Bitrix's
    own REST error envelope (``{"error": "<code>", "error_description":
    "<text>"}``) carries that reason. Prefers it when the transport
    captured a response body (see ``BoundedHttpClient.request``'s
    ``metadata["response_body"]``); falls back to the bare category if the
    body is missing/unparseable/not the expected shape -- never fabricates
    a reason the response didn't actually contain, and never echoes an
    unbounded body verbatim."""
    category = str(exc.category.value)
    body = exc.metadata.get("response_body") if isinstance(exc.metadata, dict) else None
    if not body:
        return category
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return category
    if not isinstance(parsed, dict):
        return category
    code = parsed.get("error")
    description = parsed.get("error_description")
    if code is None and description is None:
        return category
    detail = " — ".join(str(part) for part in (code, description) if part not in (None, ""))[:300]
    return f"{category}: {detail}" if detail else category


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
            if exc.category in {ProviderErrorCategory.AUTHENTICATION_FAILED, ProviderErrorCategory.AUTHORIZATION_FAILED}:
                from integrations.activation.errors import IntegrationAuthFailedError

                raise IntegrationAuthFailedError() from exc
            raise BitrixIntegrationError(_describe_provider_error(exc)) from exc

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
