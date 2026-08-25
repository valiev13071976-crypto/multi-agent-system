import unittest

from tools.gateway import SearchTimeoutError, ToolGateway
from tools.models import (
    MAX_SEARCH_RESULTS_PER_CLAIM,
    MAX_TOTAL_SEARCH_RESULTS,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
)
from tools.search.fake_provider import FakeSearchProvider, fake_result
from tools.search.http_provider import SearchUnavailableError
from tools.url_safety import is_safe_http_url


class ToolGatewayTests(unittest.IsolatedAsyncioTestCase):

    async def test_read_only_trust_level(self):
        gateway = ToolGateway(FakeSearchProvider())
        self.assertEqual(gateway.tool_trust_level, TOOL_TRUST_READ_ONLY_EXTERNAL)

    async def test_unsafe_urls_are_dropped(self):
        fake = FakeSearchProvider(
            {
                "WidgetIndex": [
                    fake_result("http://127.0.0.1/internal", snippet="WidgetIndex 12.5%"),
                    fake_result(
                        "https://en.wikipedia.org/wiki/WidgetIndex",
                        snippet="WidgetIndex 12.5%",
                    ),
                ]
            }
        )
        gateway = ToolGateway(fake)
        rows = await gateway.search("WidgetIndex reached 12.5%")
        self.assertEqual(len(rows), 1)
        self.assertTrue(is_safe_http_url(rows[0].url))

    async def test_timeout_raises_search_timeout(self):
        fake = FakeSearchProvider({"gdp": [fake_result("https://en.wikipedia.org/wiki/GDP")]})
        fake.delay_seconds = 1
        gateway = ToolGateway(fake, timeout_seconds=0.01)
        with self.assertRaises(SearchTimeoutError):
            await gateway.search("gdp grew 3%")

    async def test_provider_error_is_unavailable(self):
        fake = FakeSearchProvider()
        fake.error = RuntimeError("upstream failed")
        gateway = ToolGateway(fake)
        with self.assertRaises(SearchUnavailableError):
            await gateway.search("gdp grew 3%")

    async def test_p_max_total_search_results_enforced(self):
        rows = [
            fake_result(f"https://en.wikipedia.org/wiki/{index}", snippet=f"claim {index}")
            for index in range(8)
        ]
        fake = FakeSearchProvider({"claim": rows})
        gateway = ToolGateway(fake, max_total_results=2, max_results_per_call=5)
        first = await gateway.search("claim 1", max_results=5)
        second = await gateway.search("claim 2", max_results=5)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 0)
        self.assertLessEqual(len(first) + len(second), MAX_TOTAL_SEARCH_RESULTS)
        self.assertLessEqual(gateway._max_results_per_call, MAX_SEARCH_RESULTS_PER_CLAIM)

    async def test_empty_or_redacted_query_skips_provider(self):
        fake = FakeSearchProvider()
        gateway = ToolGateway(fake)
        rows = await gateway.search("   ")
        self.assertEqual(rows, [])
        self.assertEqual(fake.queries, [])


if __name__ == "__main__":
    unittest.main()
