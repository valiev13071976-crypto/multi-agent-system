"""Runtime selection of SearchProvider from environment configuration
(tools.search.factory.build_search_provider) -- the missing wiring step
identified while verifying PR #49's production readiness: previously
SEARCH_PROVIDER / SEARCH_API_KEY existed only as unused placeholders and
ToolGateway.search() always resolved to NullSearchProvider regardless of
configuration.
"""

from __future__ import annotations

import unittest

from tools.search.brave_provider import BraveSearchProvider
from tools.search.factory import _FailClosedSearchProvider, build_search_provider
from tools.search.http_provider import SearchUnavailableError
from tools.search.null_provider import NullSearchProvider


def run(coro):
    import asyncio

    return asyncio.run(coro)


class BuildSearchProviderTests(unittest.TestCase):
    def test_unset_search_provider_preserves_existing_null_default(self):
        provider = build_search_provider({})
        self.assertIsInstance(provider, NullSearchProvider)

    def test_missing_env_dict_falls_back_to_process_environment(self):
        # Only asserts it never raises when env=None -- must not depend on
        # this test process's actual environment variables being unset.
        provider = build_search_provider(None)
        self.assertTrue(hasattr(provider, "search"))

    def test_brave_with_valid_key_returns_real_brave_provider(self):
        provider = build_search_provider({"SEARCH_PROVIDER": "brave", "SEARCH_API_KEY": "real-key-abc123"})
        self.assertIsInstance(provider, BraveSearchProvider)

    def test_provider_name_is_case_insensitive(self):
        provider = build_search_provider({"SEARCH_PROVIDER": "BRAVE", "SEARCH_API_KEY": "real-key-abc123"})
        self.assertIsInstance(provider, BraveSearchProvider)

    def test_brave_without_api_key_fails_safe_not_crash(self):
        provider = build_search_provider({"SEARCH_PROVIDER": "brave"})
        self.assertIsInstance(provider, _FailClosedSearchProvider)
        with self.assertRaises(SearchUnavailableError) as ctx:
            run(provider.search("query"))
        self.assertIn("search_api_key_missing", str(ctx.exception))

    def test_brave_with_placeholder_api_key_fails_safe(self):
        provider = build_search_provider({"SEARCH_PROVIDER": "brave", "SEARCH_API_KEY": "changeme"})
        self.assertIsInstance(provider, _FailClosedSearchProvider)
        with self.assertRaises(SearchUnavailableError):
            run(provider.search("query"))

    def test_brave_with_empty_string_api_key_fails_safe(self):
        provider = build_search_provider({"SEARCH_PROVIDER": "brave", "SEARCH_API_KEY": ""})
        self.assertIsInstance(provider, _FailClosedSearchProvider)

    def test_unsupported_provider_name_fails_safe_not_crash(self):
        provider = build_search_provider({"SEARCH_PROVIDER": "bing", "SEARCH_API_KEY": "some-key"})
        self.assertIsInstance(provider, _FailClosedSearchProvider)
        with self.assertRaises(SearchUnavailableError) as ctx:
            run(provider.search("query"))
        self.assertIn("search_provider_unsupported", str(ctx.exception))

    def test_fail_closed_provider_never_returns_empty_list_silently(self):
        # A misconfigured provider must be observably different from "no
        # results found" -- it always raises, never returns [].
        provider = build_search_provider({"SEARCH_PROVIDER": "brave"})
        with self.assertRaises(SearchUnavailableError):
            run(provider.search("anything"))

    def test_never_crashes_construction_regardless_of_configuration(self):
        for env in (
            {},
            {"SEARCH_PROVIDER": ""},
            {"SEARCH_PROVIDER": "brave"},
            {"SEARCH_PROVIDER": "brave", "SEARCH_API_KEY": "x"},
            {"SEARCH_PROVIDER": "unsupported-vendor"},
        ):
            try:
                build_search_provider(env)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"build_search_provider raised for env={env}: {exc!r}")


if __name__ == "__main__":
    unittest.main()
