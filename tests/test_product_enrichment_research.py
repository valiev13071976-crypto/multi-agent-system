"""Product enrichment pipeline: trusted product research (requirement 2)
composed from EXISTING governed search/fetch capabilities. Fails SAFE on
any search/fetch problem -- never hallucinates a fact (requirement 15)."""

from __future__ import annotations

import unittest

from product_enrichment.identity import resolve_identity
from product_enrichment.models import (
    ProductIdentityQuery,
    SOURCE_AUTHORIZED_DISTRIBUTOR,
    SOURCE_MANUFACTURER,
    SOURCE_MANUFACTURER_DOCUMENTATION,
    SOURCE_RETAIL_CATALOG,
    SOURCE_UNKNOWN,
)
from product_enrichment.observability import EnrichmentObserver
from product_enrichment.research import classify_source_type, research_product
from tools.search.fake_provider import FakeSearchProvider, fake_result


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _identity():
    return resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))


class ClassifySourceTypeTests(unittest.TestCase):
    def test_manufacturer_domain_classified_as_manufacturer(self):
        self.assertEqual(classify_source_type("https://www.lg.com/ru/tv/55mrgb86b6a", brand="LG"), SOURCE_MANUFACTURER)

    def test_manufacturer_manual_url_classified_as_documentation(self):
        self.assertEqual(
            classify_source_type("https://www.lg.com/ru/support/manual.pdf", brand="LG"),
            SOURCE_MANUFACTURER_DOCUMENTATION,
        )

    def test_known_authorized_distributor_domain_classified_correctly(self):
        self.assertEqual(classify_source_type("https://www.citilink.ru/product/x", brand="LG"), SOURCE_AUTHORIZED_DISTRIBUTOR)

    def test_unknown_retail_domain_classified_as_retail_catalog(self):
        self.assertEqual(classify_source_type("https://some-random-shop.example/x", brand="LG"), SOURCE_RETAIL_CATALOG)

    def test_unresolvable_url_classified_as_unknown(self):
        self.assertEqual(classify_source_type("not a url", brand="LG"), SOURCE_UNKNOWN)

    def test_brand_with_no_manufacturer_table_entry_never_gets_manufacturer_tier(self):
        self.assertNotEqual(classify_source_type("https://obscurebrand.com/x", brand="ObscureBrand"), SOURCE_MANUFACTURER)


class _FakeFetchPort:
    def __init__(self, text_by_url: dict[str, str] | None = None, *, error_urls=()):
        self.text_by_url = text_by_url or {}
        self.error_urls = set(error_urls)
        self.requested: list[str] = []

    async def fetch_text(self, url: str) -> str:
        self.requested.append(url)
        if url in self.error_urls:
            raise RuntimeError("simulated fetch failure")
        return self.text_by_url.get(url, "")


class ResearchProductTests(unittest.TestCase):
    def test_search_failure_yields_empty_facts_never_hallucinated(self):
        class BrokenSearch:
            async def search(self, query, max_results=5):
                raise RuntimeError("search unavailable")

        facts = _run(research_product(_identity(), search_port=BrokenSearch(), fetch_port=_FakeFetchPort()))
        self.assertEqual(facts, ())

    def test_evidence_not_matching_identity_is_rejected(self):
        search = FakeSearchProvider(
            {"LG 55MRGB86B6A.ARUG": [fake_result("https://lg.com/other-model", title="LG 65XYZ review")]}
        )
        facts = _run(
            research_product(
                _identity(),
                search_port=search,
                fetch_port=_FakeFetchPort({"https://lg.com/other-model": "Диагональ экрана: 165 см"}),
            )
        )
        self.assertEqual(facts, ())

    def test_matching_evidence_with_recognizable_spec_line_produces_a_fact(self):
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-specs"
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": [fake_result(url, title="LG 55MRGB86B6A.ARUG specs")]})
        fetch = _FakeFetchPort({url: "Диагональ экрана: 139 см\nЦвет: черный"})
        facts = _run(research_product(_identity(), search_port=search, fetch_port=fetch))
        self.assertTrue(any(f.characteristic_key == "screen_diagonal_cm" and f.normalized_value == "139" for f in facts))
        self.assertTrue(any(f.characteristic_key == "color" for f in facts))
        self.assertTrue(all(f.source_url == url for f in facts))

    def test_variant_conflict_in_page_text_rejects_that_result_entirely(self):
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-review"
        identity = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": [fake_result(url, title="LG 55MRGB86B6A.ARUG 65-inch variant review")]})
        facts = _run(research_product(identity, search_port=search, fetch_port=_FakeFetchPort({url: "irrelevant"})))
        self.assertEqual(facts, ())

    def test_one_fetch_failure_does_not_abort_other_results(self):
        good_url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-good"
        bad_url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-bad"
        search = FakeSearchProvider(
            {
                "LG 55MRGB86B6A.ARUG": [
                    fake_result(bad_url, title="LG 55MRGB86B6A.ARUG bad source"),
                    fake_result(good_url, title="LG 55MRGB86B6A.ARUG good source"),
                ]
            }
        )
        fetch = _FakeFetchPort({good_url: "Цвет: черный"}, error_urls=(bad_url,))
        facts = _run(research_product(_identity(), search_port=search, fetch_port=fetch))
        self.assertTrue(any(f.source_url == good_url for f in facts))
        self.assertFalse(any(f.source_url == bad_url for f in facts))

    def test_empty_page_text_produces_no_facts_and_no_crash(self):
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-empty"
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": [fake_result(url, title="LG 55MRGB86B6A.ARUG")]})
        facts = _run(research_product(_identity(), search_port=search, fetch_port=_FakeFetchPort({url: ""})))
        self.assertEqual(facts, ())

    def test_observer_emits_source_accepted_and_rejected_stages(self):
        accepted_url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-a"
        rejected_url = "https://unrelated.example/other-model"
        search = FakeSearchProvider(
            {
                "LG 55MRGB86B6A.ARUG": [
                    fake_result(accepted_url, title="LG 55MRGB86B6A.ARUG specs"),
                    fake_result(rejected_url, title="unrelated"),
                ]
            }
        )
        observer = EnrichmentObserver()
        _run(
            research_product(
                _identity(),
                search_port=search,
                fetch_port=_FakeFetchPort({accepted_url: "Цвет: черный"}),
                observer=observer,
            )
        )
        stages = observer.stages()
        self.assertIn("source_accepted", stages)
        self.assertIn("source_rejected", stages)


if __name__ == "__main__":
    unittest.main()
