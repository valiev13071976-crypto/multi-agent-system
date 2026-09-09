"""Production defect closure: a governed ``scrape.fetch`` of a REAL
product page succeeded (HTTP 200) yet its consumer received no content.

``tools.gateway.bound_result_data`` replaces a result payload larger than
``MAX_TOOL_RESULT_DATA_BYTES`` (64 KB) with a ``{"truncated": True,
"keys": [...]}`` stub. For a page-fetch tool the payload IS the page, and
every real catalog page is 100 KB+, so ``ToolResult.data`` came back
WITHOUT ``body_text``: ``product_enrichment.research.
ToolGatewayResearchAdapter.fetch_text`` then returned ``""`` and research
rejected every source as ``empty_page`` -- zero characteristics, zero
media candidates and a fact-free description, while the Railway logs
showed four successful 200 fetches.

The fetch tool now declares its own result-data bound (its body size is
already bounded by the adapter's ``max_response_bytes``); tools that
declare nothing keep the generic 64 KB cap.
"""

from __future__ import annotations

import unittest

import httpx

from autonomy.capabilities import CAP_EXTERNAL_READ, CAP_SCRAPE, CapabilitySet
from autonomy.models import utc_now
from product_enrichment.identity import resolve_identity
from product_enrichment.models import ProductIdentityQuery
from product_enrichment.research import ToolGatewayResearchAdapter, research_product
from tools.gateway import ToolGateway, bound_result_data, result_data_bound_for
from tools.models import (
    MAX_TOOL_PAGE_RESULT_DATA_BYTES,
    MAX_TOOL_RESULT_DATA_BYTES,
    ToolRequest,
)
from tools.platform.descriptors import scrape_fetch_descriptor, seo_analytics_read_descriptor
from tools.platform.web_fetch_adapter import WebFetchAdapter
from tools.registry import ToolRegistry
from tools.search.fake_provider import FakeSearchProvider, fake_result

PAGE_URL = "https://shop-three.example/catalog/acme-model-55x"
BRAND = "ACME"
MODEL = "MODEL-55X"

# A page the size real catalog pages actually are (>64 KB), whose
# specification block sits far past the old bound -- exactly like the
# production pages, where the first spec row appeared ~180 KB in.
_FILLER = '<div class="marketing">Описание и преимущества модели. </div>' * 3000
LARGE_PAGE_HTML = (
    "<html><head>"
    '<meta property="og:image" content="https://shop-three.example/img/hero.jpg">'
    f"</head><body><h1>{BRAND} {MODEL}</h1>"
    f"{_FILLER}"
    '<table class="specs"><tbody>'
    "<tr><th>Диагональ экрана</th><td>55&quot;</td></tr>"
    "<tr><th>Разрешение экрана</th><td>3840x2160</td></tr>"
    "<tr><th>Частота обновления</th><td>120&nbsp;Гц</td></tr>"
    "</tbody></table></body></html>"
)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _page_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LARGE_PAGE_HTML, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)


def _gateway(search_provider=None) -> ToolGateway:
    registry = ToolRegistry()
    registry.register(
        scrape_fetch_descriptor(enabled=True), adapter=WebFetchAdapter(transport=_page_transport())
    )
    return ToolGateway(search_provider, registry=registry, register_search=False)


def _fetch_request() -> tuple[ToolRequest, CapabilitySet]:
    caps = (CAP_SCRAPE, CAP_EXTERNAL_READ)
    request = ToolRequest(
        request_id="req-1",
        workflow_id="wf-1",
        task_id="task-1",
        tool_id="scrape.fetch",
        operation="fetch",
        arguments={"url": PAGE_URL},
        tenant_id="tenant-a",
        requested_capabilities=caps,
    )
    return request, CapabilitySet(subject_id="test", capabilities=caps, issued_at=utc_now())


class PageIsLargerThanTheGenericBoundTests(unittest.TestCase):
    def test_fixture_page_exceeds_the_generic_result_bound(self):
        self.assertGreater(len(LARGE_PAGE_HTML.encode("utf-8")), MAX_TOOL_RESULT_DATA_BYTES)


class ResultDataBoundTests(unittest.TestCase):
    def test_page_fetch_tool_declares_a_larger_result_bound(self):
        self.assertEqual(
            result_data_bound_for(scrape_fetch_descriptor(enabled=True)),
            MAX_TOOL_PAGE_RESULT_DATA_BYTES,
        )

    def test_tool_declaring_nothing_keeps_the_generic_bound(self):
        self.assertEqual(
            result_data_bound_for(seo_analytics_read_descriptor(enabled=True)),
            MAX_TOOL_RESULT_DATA_BYTES,
        )

    def test_oversized_payload_is_still_collapsed_under_the_generic_bound(self):
        bounded = bound_result_data({"body_text": "x" * (MAX_TOOL_RESULT_DATA_BYTES + 1)})
        self.assertTrue(bounded.get("truncated"))
        self.assertNotIn("body_text", bounded)

    def test_oversized_payload_survives_under_an_explicit_larger_bound(self):
        payload = {"body_text": "x" * (MAX_TOOL_RESULT_DATA_BYTES + 1)}
        bounded = bound_result_data(payload, max_bytes=MAX_TOOL_PAGE_RESULT_DATA_BYTES)
        self.assertEqual(bounded["body_text"], payload["body_text"])
        self.assertNotIn("truncated", bounded)

    def test_payload_beyond_even_the_larger_bound_is_still_collapsed(self):
        payload = {"body_text": "x" * (MAX_TOOL_PAGE_RESULT_DATA_BYTES + 10)}
        bounded = bound_result_data(payload, max_bytes=MAX_TOOL_PAGE_RESULT_DATA_BYTES)
        self.assertTrue(bounded.get("truncated"))


class GovernedPageFetchReturnsTheBodyTests(unittest.TestCase):
    def test_gateway_invoke_returns_the_whole_page_body(self):
        request, caps = _fetch_request()
        result = _run(_gateway().invoke(request, capabilities=caps))
        self.assertTrue(result.success)
        # ``truncated`` here is the ADAPTER's own byte-bound flag, false for
        # this page; the gateway must not have collapsed the payload.
        self.assertFalse(result.data["truncated"])
        self.assertEqual(result.data["body_text"], LARGE_PAGE_HTML)

    def test_research_adapter_fetch_text_returns_the_whole_page(self):
        adapter = ToolGatewayResearchAdapter(_gateway(), tenant_id="tenant-a")
        text = _run(adapter.fetch_text(PAGE_URL))
        self.assertEqual(text, LARGE_PAGE_HTML)

    def test_research_over_a_real_sized_page_produces_facts_and_media(self):
        search = FakeSearchProvider(
            {
                f"{BRAND} {MODEL} характеристики specifications": [
                    fake_result(PAGE_URL, title=f"{BRAND} {MODEL}", snippet=f"{BRAND} {MODEL} характеристики")
                ]
            }
        )
        gateway = _gateway(search)
        adapter = ToolGatewayResearchAdapter(gateway, tenant_id="tenant-a")
        media: list = []
        facts = _run(
            research_product(
                resolve_identity(ProductIdentityQuery(brand=BRAND, model=MODEL, article=MODEL)),
                search_port=adapter,
                fetch_port=adapter,
                media_sink=media,
            )
        )
        keys = {fact.characteristic_key for fact in facts}
        self.assertIn("screen_resolution", keys)
        self.assertIn("refresh_rate_hz", keys)
        self.assertEqual([candidate.url for candidate in media], ["https://shop-three.example/img/hero.jpg"])


if __name__ == "__main__":
    unittest.main()
