"""Runtime selection of ``SearchProvider`` from environment configuration.

Reads ``SEARCH_PROVIDER`` / ``SEARCH_API_KEY`` (``production_foundation.
secrets``'s existing "Search API" OPTIONAL secret entry) exactly once, at
``ToolGateway`` construction time (see ``side_effects.runtime.
build_tool_gateway``) -- it never hardcodes a vendor default and never
replaces ``ToolGateway``/the ``SearchProvider`` abstraction itself.

- ``SEARCH_PROVIDER`` unset/empty: preserves the EXISTING behavior
  (``NullSearchProvider`` -- quiet, empty results) exactly as before this
  module existed. Absence of configuration is not an error.
- ``SEARCH_PROVIDER`` set to an unsupported value, or ``SEARCH_PROVIDER=
  brave`` without a real (non-placeholder) ``SEARCH_API_KEY``: fails
  SAFELY -- never raises during construction (so a misconfigured/missing
  key never breaks application startup), but the returned provider's
  ``.search()`` always raises ``SearchUnavailableError`` with a
  diagnosable, non-secret reason code the moment it is actually invoked.
  This is deliberately distinct from the "unset" case above: an explicit
  but broken configuration must never be silently indistinguishable from
  "no results found" -- see ``_FailClosedSearchProvider``.
- ``SEARCH_PROVIDER=brave`` with a valid key: returns a real
  ``BraveSearchProvider``.
"""

from __future__ import annotations

import os

from production_foundation.config import reject_placeholder_secret
from tools.models import SearchResult
from tools.search.base import SearchProvider
from tools.search.brave_provider import BraveSearchProvider
from tools.search.http_provider import SearchUnavailableError
from tools.search.null_provider import NullSearchProvider

SUPPORTED_SEARCH_PROVIDERS = ("brave",)


class _FailClosedSearchProvider:
    """Returned only when ``SEARCH_PROVIDER`` is explicitly set but
    misconfigured/unsupported. ``.search()`` always raises -- it never
    returns ``[]`` -- so a broken configuration is never mistaken for a
    query that legitimately found nothing (module docstring: "do not
    silently claim internet research succeeded when no live provider is
    configured")."""

    def __init__(self, reason_code: str):
        self._reason_code = reason_code

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise SearchUnavailableError(self._reason_code)


def build_search_provider(env: dict | None = None) -> SearchProvider:
    """Builds the ``SearchProvider`` ``ToolGateway`` should be constructed
    with, from environment configuration. Never raises -- construction
    failures degrade to a fail-closed provider (see module docstring)."""
    source = env if env is not None else os.environ
    provider_name = str(source.get("SEARCH_PROVIDER") or "").strip().casefold()
    if not provider_name:
        return NullSearchProvider()
    if provider_name not in SUPPORTED_SEARCH_PROVIDERS:
        return _FailClosedSearchProvider(f"search_provider_unsupported:{provider_name}")

    if provider_name == "brave":
        api_key = str(source.get("SEARCH_API_KEY") or "").strip()
        if not api_key or not reject_placeholder_secret(api_key):
            return _FailClosedSearchProvider("search_api_key_missing")
        try:
            return BraveSearchProvider(api_key=api_key)
        except ValueError:
            return _FailClosedSearchProvider("search_api_key_missing")

    # Unreachable given SUPPORTED_SEARCH_PROVIDERS above; kept as an
    # explicit fail-closed fallback rather than an assert/crash.
    return _FailClosedSearchProvider(f"search_provider_unsupported:{provider_name}")
