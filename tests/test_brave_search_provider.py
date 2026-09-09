"""Production Brave Search provider (Railway SEARCH_PROVIDER=brave /
SEARCH_API_KEY wiring). Exercises request/auth/header construction and
result normalization against a fully mocked httpx.MockTransport -- same
test pattern as WebFetchAdapter / GovernedImageFetcher -- never a real
network call.
"""

from __future__ import annotations

import unittest

import httpx

from tools.models import ALLOWED_TRUST
from tools.search.brave_provider import BraveSearchProvider
from tools.search.http_provider import SearchUnavailableError


def run(coro):
    import asyncio

    return asyncio.run(coro)


def capturing_handler(payload, captured):
    def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["method"] = request.method
        return httpx.Response(200, json=payload)

    return handler


class BraveSearchProviderConstructionTests(unittest.TestCase):
    def test_missing_api_key_raises_at_construction(self):
        with self.assertRaises(ValueError):
            BraveSearchProvider(api_key="")

    def test_whitespace_only_api_key_raises_at_construction(self):
        with self.assertRaises(ValueError):
            BraveSearchProvider(api_key="   ")


class BraveSearchProviderRequestConstructionTests(unittest.TestCase):
    def test_request_uses_the_documented_brave_web_search_endpoint(self):
        captured = {}
        handler = capturing_handler({"web": {"results": []}}, captured)
        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        run(provider.search("LG 55MRGB86B6A specifications"))
        self.assertTrue(captured["url"].startswith("https://api.search.brave.com/res/v1/web/search"))
        self.assertEqual(captured["method"], "GET")

    def test_query_and_count_are_sent_as_query_parameters(self):
        captured = {}
        handler = capturing_handler({"web": {"results": []}}, captured)
        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        run(provider.search("LG 55MRGB86B6A", max_results=7))
        normalized_url = captured["url"].replace("%20", "+")
        self.assertIn("q=LG+55MRGB86B6A", normalized_url)
        self.assertIn("count=7", captured["url"])

    def test_count_is_bounded_to_brave_api_max(self):
        captured = {}
        handler = capturing_handler({"web": {"results": []}}, captured)
        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        run(provider.search("query", max_results=999))
        self.assertIn("count=20", captured["url"])

    def test_api_key_is_sent_only_via_the_subscription_token_header(self):
        captured = {}
        handler = capturing_handler({"web": {"results": []}}, captured)
        provider = BraveSearchProvider(api_key="super-secret-token", transport=httpx.MockTransport(handler))
        run(provider.search("query"))
        self.assertEqual(captured["headers"].get("x-subscription-token"), "super-secret-token")
        self.assertNotIn("super-secret-token", captured["url"])

    def test_accept_json_header_is_sent(self):
        captured = {}
        handler = capturing_handler({"web": {"results": []}}, captured)
        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        run(provider.search("query"))
        self.assertEqual(captured["headers"].get("accept"), "application/json")

    def test_empty_query_never_issues_a_network_call(self):
        def handler(request):
            raise AssertionError("must never be called for an empty query")

        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        results = run(provider.search("   "))
        self.assertEqual(results, [])


class BraveSearchProviderResultNormalizationTests(unittest.TestCase):
    def make_provider(self, payload):
        handler = capturing_handler(payload, {})
        return BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))

    def test_title_url_snippet_and_domain_are_preserved(self):
        payload = {
            "web": {
                "results": [
                    {
                        "title": "LG 55MRGB86B6A.ARUG specifications",
                        "url": "https://www.lg.com/ru/55mrgb86b6a",
                        "description": "Screen diagonal 139 cm",
                        "meta_url": {"hostname": "www.lg.com"},
                    }
                ]
            }
        }
        provider = self.make_provider(payload)
        results = run(provider.search("LG 55MRGB86B6A.ARUG"))
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row.title, "LG 55MRGB86B6A.ARUG specifications")
        self.assertEqual(row.url, "https://www.lg.com/ru/55mrgb86b6a")
        self.assertEqual(row.snippet, "Screen diagonal 139 cm")
        self.assertEqual(row.source_domain, "www.lg.com")
        self.assertIn(row.trust_level, ALLOWED_TRUST)

    def test_html_markup_in_description_is_stripped_from_snippet(self):
        payload = {
            "web": {
                "results": [
                    {
                        "title": "LG TV specs",
                        "url": "https://www.lg.com/x",
                        "description": "Diagonal <strong>139</strong> cm",
                    }
                ]
            }
        }
        provider = self.make_provider(payload)
        results = run(provider.search("LG TV"))
        self.assertEqual(results[0].snippet, "Diagonal 139 cm")

    def test_results_missing_title_or_url_are_dropped(self):
        payload = {
            "web": {
                "results": [
                    {"title": "", "url": "https://www.lg.com/x", "description": "d"},
                    {"title": "No URL", "url": "", "description": "d"},
                    {"title": "Valid", "url": "https://www.lg.com/y", "description": "d"},
                ]
            }
        }
        provider = self.make_provider(payload)
        results = run(provider.search("query"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Valid")

    def test_results_are_capped_to_requested_max_results(self):
        payload = {
            "web": {
                "results": [
                    {"title": "Result %d" % i, "url": "https://example.com/%d" % i, "description": ""}
                    for i in range(10)
                ]
            }
        }
        provider = self.make_provider(payload)
        results = run(provider.search("query", max_results=3))
        self.assertEqual(len(results), 3)

    def test_missing_web_key_returns_empty_list_not_an_error(self):
        provider = self.make_provider({"query": {"original": "x"}})
        results = run(provider.search("query"))
        self.assertEqual(results, [])

    def test_non_list_results_returns_empty_list_not_an_error(self):
        provider = self.make_provider({"web": {"results": "not-a-list"}})
        results = run(provider.search("query"))
        self.assertEqual(results, [])


class BraveSearchProviderErrorHandlingTests(unittest.TestCase):
    def test_401_response_raises_search_unavailable_with_auth_reason(self):
        def handler(request):
            return httpx.Response(401, json={"error": "unauthorized"})

        provider = BraveSearchProvider(api_key="bad-key", transport=httpx.MockTransport(handler))
        with self.assertRaises(SearchUnavailableError) as ctx:
            run(provider.search("query"))
        self.assertIn("brave_auth_rejected", str(ctx.exception))

    def test_403_response_raises_search_unavailable(self):
        def handler(request):
            return httpx.Response(403, json={"error": "forbidden"})

        provider = BraveSearchProvider(api_key="bad-key", transport=httpx.MockTransport(handler))
        with self.assertRaises(SearchUnavailableError):
            run(provider.search("query"))

    def test_429_response_raises_search_unavailable_with_rate_limit_reason(self):
        def handler(request):
            return httpx.Response(429, json={"error": "rate_limited"})

        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        with self.assertRaises(SearchUnavailableError) as ctx:
            run(provider.search("query"))
        self.assertIn("brave_rate_limited", str(ctx.exception))

    def test_500_response_raises_search_unavailable(self):
        def handler(request):
            return httpx.Response(500, text="internal error")

        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        with self.assertRaises(SearchUnavailableError) as ctx:
            run(provider.search("query"))
        self.assertIn("brave_http_status_500", str(ctx.exception))

    def test_malformed_json_response_raises_search_unavailable(self):
        def handler(request):
            return httpx.Response(200, text="not json at all")

        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        with self.assertRaises(SearchUnavailableError) as ctx:
            run(provider.search("query"))
        self.assertIn("brave_malformed_response", str(ctx.exception))

    def test_non_dict_json_response_raises_search_unavailable(self):
        def handler(request):
            return httpx.Response(200, json=["not", "a", "dict"])

        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        with self.assertRaises(SearchUnavailableError):
            run(provider.search("query"))

    def test_network_error_raises_search_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        provider = BraveSearchProvider(api_key="secret-key-123", transport=httpx.MockTransport(handler))
        with self.assertRaises(SearchUnavailableError) as ctx:
            run(provider.search("query"))
        self.assertIn("brave_network_error", str(ctx.exception))

    def test_api_key_never_appears_in_any_raised_exception_message(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        provider = BraveSearchProvider(api_key="super-secret-value", transport=httpx.MockTransport(handler))
        with self.assertRaises(SearchUnavailableError) as ctx:
            run(provider.search("query"))
        self.assertNotIn("super-secret-value", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
