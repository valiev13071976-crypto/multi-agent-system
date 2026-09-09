"""Production Brave Search provider.

This is the ONE missing piece identified while verifying PR #49's
production readiness: ``ToolGateway.search()`` always resolved to
``NullSearchProvider`` in production because no adapter for
``SEARCH_PROVIDER=brave`` existed anywhere in the repository -- the
``SEARCH_PROVIDER``/``SEARCH_API_KEY`` env vars were documented (see
``production_foundation/secrets.py``'s "Search API" entry) but never read
by any code. This module implements the EXISTING ``tools.search.base.
SearchProvider`` protocol only -- it is not a new tool, gateway, or fetch
path. Selection/wiring from environment lives in
``tools.search.factory.build_search_provider``.

Endpoint: ``GET https://api.search.brave.com/res/v1/web/search``
Auth: ``X-Subscription-Token`` header (never the query string, never
logged/included in any exception message).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from tools.models import TRUST_UNKNOWN, SearchResult
from tools.search.http_provider import SearchUnavailableError

BRAVE_WEB_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_TIMEOUT_SECONDS = 8.0
# Brave's own per-request result cap (documented API limit).
_MAX_BRAVE_COUNT = 20

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str) -> str:
    """Brave's ``description`` field may contain ``<strong>`` highlight
    markup; downstream identity/spec matching (``product_enrichment``)
    works on plain text, so tags are stripped defensively -- never trusted
    as real HTML."""
    return _HTML_TAG_RE.sub("", value or "").strip()


class BraveSearchProvider:
    """Adapts the Brave Web Search API to the existing ``SearchProvider``
    protocol (``async def search(query, max_results) -> list[SearchResult]``).

    Real callers never construct this directly with network-facing
    intent bypassing ``ToolGateway`` -- it is wired exclusively through
    ``tools.search.factory.build_search_provider`` -> ``ToolGateway.
    __init__(search_provider=...)``, so every call still passes through
    ``ToolGateway.search()``'s existing redaction / URL-safety / budget
    enforcement (this provider's own results are re-validated there
    exactly like any other provider's).

    Tests inject a deterministic ``transport`` (``httpx.MockTransport``)
    -- the SAME pattern already used by ``WebFetchAdapter`` /
    ``GovernedImageFetcher`` -- instead of ever hitting the network.
    """

    provider_id = "brave"

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        endpoint: str = BRAVE_WEB_SEARCH_ENDPOINT,
    ):
        if not str(api_key or "").strip():
            # Construction-time fail-closed -- mirrors
            # ``OpenAIImageGenerationProvider.__post_init__``'s own
            # "never construct a real provider without its key" contract.
            raise ValueError("brave_api_key_required")
        self._api_key = str(api_key).strip()
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport
        self._endpoint = endpoint

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        cleaned = str(query or "").strip()
        if not cleaned:
            return []
        count = max(1, min(int(max_results or 5), _MAX_BRAVE_COUNT))
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = await client.get(
                    self._endpoint,
                    params={"q": cleaned, "count": count},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise SearchUnavailableError("brave_network_error") from exc

        if response.status_code in (401, 403):
            raise SearchUnavailableError("brave_auth_rejected")
        if response.status_code == 429:
            raise SearchUnavailableError("brave_rate_limited")
        if response.status_code >= 400:
            raise SearchUnavailableError(f"brave_http_status_{response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchUnavailableError("brave_malformed_response") from exc
        if not isinstance(payload, dict):
            raise SearchUnavailableError("brave_unsupported_response_shape")

        web = payload.get("web")
        items = web.get("results") if isinstance(web, dict) else None
        if not isinstance(items, list):
            return []

        retrieved_at = datetime.now(timezone.utc)
        results: list[SearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue
            snippet = _strip_html(str(item.get("description") or ""))
            domain = ""
            meta_url = item.get("meta_url")
            if isinstance(meta_url, dict):
                domain = str(meta_url.get("hostname") or "").strip()
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    # trust_level is intentionally the neutral default --
                    # ToolGateway.search() recomputes it from the URL's
                    # domain via tools.trust.trust_for_domain() for EVERY
                    # provider's results, so a provider-supplied value is
                    # never actually trusted on its own.
                    source_domain=domain,
                    published_at=None,
                    retrieved_at=retrieved_at,
                    trust_level=TRUST_UNKNOWN,
                )
            )
            if len(results) >= count:
                break
        return results
