"""Allowlisted HTTP adapter with SSRF controls."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx

from tools.integration import IntegrationCredentialRef
from tools.errors import (
    ToolArgumentInvalidError,
    ToolAuthFailedError,
    ToolPermanentFailureError,
    ToolRateLimitedError,
    ToolTimeoutError,
)
from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE
from tools.url_safety import UnsafeUrlError, validate_http_url


class HttpAdapter:
    adapter_id = "http"

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...] = (),
        max_response_bytes: int = 65536,
        timeout_seconds: float = 10.0,
        credential_store=None,
    ):
        self._allowed_hosts = frozenset(h.lower() for h in allowed_hosts if h)
        self._max_bytes = int(max_response_bytes)
        self._timeout = float(timeout_seconds)
        self._credentials = credential_store

    def supports(self, tool_id: str) -> bool:
        return tool_id == "http.request"

    def health(self) -> str:
        return ADAPTER_HEALTHY if self._allowed_hosts else ADAPTER_UNAVAILABLE

    def _check_host(self, url: str) -> None:
        try:
            validate_http_url(url)
        except UnsafeUrlError as exc:
            raise ToolArgumentInvalidError("tool_argument_invalid") from exc
        host = (urlparse(url).hostname or "").lower()
        if host not in self._allowed_hosts:
            raise ToolAuthFailedError("tool_permission_denied")

    async def execute_read(self, request, context) -> dict:
        args = dict(request.arguments or {})
        url = str(args.get("url") or "")
        if not url:
            raise ToolArgumentInvalidError()
        self._check_host(url)
        integration_id = str(args.get("integration_id") or "")
        headers = {}
        if integration_id and self._credentials and request.tenant_id:
            cfg = self._credentials.get_config(request.tenant_id, integration_id)
            if cfg and cfg.credential_ref:
                secret = self._credentials.resolve_secret(
                    IntegrationCredentialRef(
                        integration_id=integration_id,
                        tenant_id=request.tenant_id,
                        credential_key=cfg.credential_ref,
                        provider=cfg.provider,
                    )
                )
                if secret:
                    headers["Authorization"] = f"Bearer {secret}"
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=self._timeout) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise ToolTimeoutError() from exc
        except httpx.HTTPError as exc:
            raise ToolPermanentFailureError() from exc
        if resp.status_code == 429:
            raise ToolRateLimitedError()
        if resp.status_code >= 400:
            raise ToolPermanentFailureError("tool_permanent_failure")
        body = resp.content[: self._max_bytes]
        return {
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "body_text": body.decode("utf-8", errors="replace"),
            "truncated": len(resp.content) > self._max_bytes,
            "provenance": {"url": url, "method": "GET"},
        }
