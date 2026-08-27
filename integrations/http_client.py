"""Controlled integration HTTP client — host-locked, SSRF-safe, auth server-side."""

from __future__ import annotations

import asyncio
from typing import Mapping
from urllib.parse import urljoin, urlparse

import httpx

from integrations.auth import apply_auth_to_url, get_auth_strategy
from integrations.circuit_breaker import CircuitBreaker
from integrations.contracts import IntegrationDescriptor, IntegrationOperationContext, TimeoutPolicy
from integrations.errors import (
    ExternalPermanentFailureError,
    ExternalRateLimitedError,
    ExternalTransientFailureError,
    HostNotAllowedError,
    IntegrationDisabledError,
    IntegrationTimeoutError,
    IpAllowlistDeniedError,
)
from tools.url_safety import UnsafeUrlError, validate_http_url


class IntegrationHttpClient:
    """Outbound HTTP for registered integrations only."""

    def __init__(
        self,
        *,
        secrets_backend,
        circuit_breaker: CircuitBreaker | None = None,
        max_response_bytes: int = 65536,
    ):
        self._secrets = secrets_backend
        self._breaker = circuit_breaker or CircuitBreaker()
        self._max_bytes = int(max_response_bytes)

    def _resolve_url(self, descriptor: IntegrationDescriptor, path_or_url: str) -> str:
        raw = str(path_or_url or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            url = raw
        else:
            base = descriptor.base_url.rstrip("/") + "/"
            url = urljoin(base, raw.lstrip("/"))
        try:
            validate_http_url(url)
        except UnsafeUrlError as exc:
            raise HostNotAllowedError("host_not_allowed") from exc
        host = (urlparse(url).hostname or "").lower()
        allowed = set(descriptor.allowed_hosts)
        if descriptor.base_url:
            base_host = (urlparse(descriptor.base_url).hostname or "").lower()
            if base_host:
                allowed.add(base_host)
        if not allowed or host not in allowed:
            raise HostNotAllowedError("host_not_allowed")
        # Model cannot retarget to arbitrary host outside allowlist
        return url

    def _check_ip_allowlist(
        self, descriptor: IntegrationDescriptor, source_ip: str | None
    ) -> None:
        """Defense-in-depth only — never authenticates by itself."""
        if not descriptor.ip_allowlist:
            return
        if not source_ip or source_ip not in descriptor.ip_allowlist:
            raise IpAllowlistDeniedError("ip_allowlist_denied")

    async def request(
        self,
        descriptor: IntegrationDescriptor,
        context: IntegrationOperationContext,
        *,
        method: str,
        path: str,
        json_body: Mapping | None = None,
        headers: Mapping[str, str] | None = None,
        source_ip: str | None = None,
        secret: str | None = None,
        retry_attempt: int = 0,
    ) -> dict:
        if not descriptor.enabled:
            raise IntegrationDisabledError("integration_disabled")
        self._check_ip_allowlist(descriptor, source_ip)
        self._breaker.assert_allow(context.tenant_id, context.integration_id)

        method_u = method.upper()
        if method_u not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ExternalPermanentFailureError("external_permanent_failure")
        if context.is_write and method_u == "GET":
            method_u = "POST"

        url = self._resolve_url(descriptor, path)
        auth_headers: dict[str, str] = {}
        if secret is not None:
            strategy = get_auth_strategy(descriptor.auth_strategy)
            material = strategy.build_auth(
                secret=secret, settings=dict(descriptor.safe_settings)
            )
            auth_headers.update(material.headers)
            url = apply_auth_to_url(url, material)

        req_headers = dict(headers or {})
        req_headers.update(auth_headers)
        if context.idempotency_key and context.is_write:
            req_headers.setdefault("Idempotency-Key", context.idempotency_key)
        req_headers.setdefault("X-Request-Id", context.request_id)

        tp: TimeoutPolicy = descriptor.timeout_policy
        timeout = httpx.Timeout(
            connect=tp.connect_seconds,
            read=tp.read_seconds,
            write=tp.read_seconds,
            pool=tp.connect_seconds,
        )
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
                resp = await asyncio.wait_for(
                    client.request(method_u, url, headers=req_headers, json=json_body),
                    timeout=tp.total_seconds,
                )
        except asyncio.TimeoutError as exc:
            self._breaker.record_failure(
                context.tenant_id, context.integration_id, error_code="integration_timeout"
            )
            raise IntegrationTimeoutError("integration_timeout") from exc
        except httpx.TimeoutException as exc:
            self._breaker.record_failure(
                context.tenant_id, context.integration_id, error_code="integration_timeout"
            )
            raise IntegrationTimeoutError("integration_timeout") from exc
        except httpx.HTTPError as exc:
            self._breaker.record_failure(
                context.tenant_id,
                context.integration_id,
                error_code="external_transient_failure",
            )
            raise ExternalTransientFailureError("external_transient_failure") from exc

        if resp.status_code == 429:
            self._breaker.record_failure(
                context.tenant_id, context.integration_id, error_code="external_rate_limited"
            )
            raise ExternalRateLimitedError("external_rate_limited")
        if resp.status_code in descriptor.retry_policy.retry_on_status:
            self._breaker.record_failure(
                context.tenant_id,
                context.integration_id,
                error_code="external_transient_failure",
            )
            raise ExternalTransientFailureError("external_transient_failure")
        if resp.status_code in {401, 403}:
            self._breaker.record_failure(
                context.tenant_id, context.integration_id, error_code="authentication_failed"
            )
            from integrations.errors import AuthenticationFailedError

            raise AuthenticationFailedError("authentication_failed")
        if resp.status_code >= 400:
            self._breaker.record_failure(
                context.tenant_id,
                context.integration_id,
                error_code="external_permanent_failure",
            )
            raise ExternalPermanentFailureError("external_permanent_failure")

        self._breaker.record_success(context.tenant_id, context.integration_id)
        body = resp.content[: self._max_bytes]
        return {
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "body_text": body.decode("utf-8", errors="replace"),
            "truncated": len(resp.content) > self._max_bytes,
            "headers": {
                k: v
                for k, v in resp.headers.items()
                if k.lower() not in {"authorization", "set-cookie", "cookie"}
            },
            "retry_attempt": retry_attempt,
        }
