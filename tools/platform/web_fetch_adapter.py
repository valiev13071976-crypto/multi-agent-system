"""SSRF-hardened public web fetch adapter — ``scrape.fetch`` tool.

Unlike ``HttpAdapter`` (fixed host allowlist, no redirect handling), this
adapter is the safe path for fetching ARBITRARY public HTTP/HTTPS pages the
user references in chat (Block 5.2 Data Acquisition & Parsing Platform).
Every URL — including every redirect hop — is revalidated through
``tools.url_safety.validate_http_url`` (scheme/host/private-IP/blocked-port
checks) before any network call is issued, so a safe-looking public URL can
never redirect into an internal/private target. No user-controlled headers
are accepted (never an open proxy). Response bytes are bounded to defend
against decompression bombs / oversized pages.
"""

from __future__ import annotations

import httpx

from tools.errors import (
    ToolArgumentInvalidError,
    ToolPermanentFailureError,
    ToolPolicyDeniedError,
    ToolRateLimitedError,
    ToolTimeoutError,
)
from tools.models import ADAPTER_HEALTHY
from tools.url_safety import UnsafeUrlError, validate_http_url

DEFAULT_USER_AGENT = "PandaAcquisitionBot/1.0 (+panda-data-acquisition)"
DEFAULT_MAX_RESPONSE_BYTES = 5_000_000
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 6.0


class WebFetchAdapter:
    """Adapter for the ``scrape.fetch`` tool — single bounded page fetch."""

    adapter_id = "scrape"

    def __init__(
        self,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._max_bytes = int(max_response_bytes)
        self._timeout = float(timeout_seconds)
        self._connect_timeout = float(connect_timeout_seconds)
        self._max_redirects = max(0, int(max_redirects))
        self._user_agent = str(user_agent or DEFAULT_USER_AGENT)
        # Injectable transport (tests use httpx.MockTransport — no live network).
        self._transport = transport

    def supports(self, tool_id: str) -> bool:
        return tool_id == "scrape.fetch"

    def health(self) -> str:
        return ADAPTER_HEALTHY

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            self._timeout, connect=self._connect_timeout
        )
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            transport=self._transport,
        )

    async def _read_bounded(self, resp: httpx.Response) -> tuple[bytes, bool]:
        """Read up to ``max_bytes`` of the response body; never load unbounded data."""

        chunks: list[bytes] = []
        total = 0
        truncated = False
        async for chunk in resp.aiter_bytes():
            if not chunk:
                continue
            remaining = self._max_bytes - total
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                total += remaining
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks), truncated

    async def execute_read(self, request, context) -> dict:
        args = dict(request.arguments or {})
        url = str(args.get("url") or "")
        if not url:
            raise ToolArgumentInvalidError()
        try:
            safe_url = validate_http_url(url)
        except UnsafeUrlError as exc:
            raise ToolPolicyDeniedError(f"unsafe_url:{exc.reason}") from exc

        visited: set[str] = set()
        current = safe_url
        redirects_followed = 0
        headers = {"User-Agent": self._user_agent, "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5"}

        async with self._client() as client:
            while True:
                if current in visited:
                    raise ToolPermanentFailureError("redirect_loop_detected")
                visited.add(current)
                try:
                    async with client.stream("GET", current, headers=headers) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            location = resp.headers.get("location") or ""
                            if not location:
                                raise ToolPermanentFailureError("redirect_missing_location")
                            if redirects_followed >= self._max_redirects:
                                raise ToolPermanentFailureError("redirect_limit_exceeded")
                            from urllib.parse import urljoin

                            next_url = urljoin(current, location)
                            try:
                                # Redirect destinations are UNTRUSTED — revalidate
                                # every hop so a safe-looking public URL can never
                                # redirect into an internal/private target.
                                next_url = validate_http_url(next_url)
                            except UnsafeUrlError as exc:
                                raise ToolPolicyDeniedError(
                                    f"unsafe_redirect:{exc.reason}"
                                ) from exc
                            redirects_followed += 1
                            current = next_url
                            continue

                        if resp.status_code == 429:
                            raise ToolRateLimitedError()
                        if resp.status_code >= 400:
                            raise ToolPermanentFailureError(
                                f"http_status_{resp.status_code}"
                            )
                        body, truncated = await self._read_bounded(resp)
                        content_type = resp.headers.get("content-type", "")
                        return {
                            "status_code": resp.status_code,
                            "content_type": content_type,
                            "body_text": body.decode("utf-8", errors="replace"),
                            "truncated": truncated,
                            "final_url": current,
                            "requested_url": safe_url,
                            "redirects_followed": redirects_followed,
                            "provenance": {
                                "url": current,
                                "requested_url": safe_url,
                                "method": "GET",
                                "adapter": "scrape",
                            },
                        }
                except httpx.TimeoutException as exc:
                    raise ToolTimeoutError() from exc
                except httpx.HTTPError as exc:
                    raise ToolPermanentFailureError("network_error") from exc
