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
from product_enrichment.research import classify_source_type, extract_image_candidate_urls, research_product
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


class ExtractImageCandidateUrlsTests(unittest.TestCase):
    def test_empty_html_returns_no_candidates(self):
        self.assertEqual(extract_image_candidate_urls("", base_url="https://lg.com/x"), ())

    def test_og_image_meta_tag_is_extracted(self):
        html = '<html><head><meta property="og:image" content="https://lg.com/hero.jpg"></head></html>'
        self.assertEqual(extract_image_candidate_urls(html, base_url="https://lg.com/x"), ("https://lg.com/hero.jpg",))

    def test_img_tag_src_is_extracted(self):
        html = '<html><body><img src="https://lg.com/photo.png" alt="tv"></body></html>'
        self.assertEqual(extract_image_candidate_urls(html, base_url="https://lg.com/x"), ("https://lg.com/photo.png",))

    def test_relative_url_is_resolved_against_base_url(self):
        html = '<html><body><img src="/img/photo.png"></body></html>'
        self.assertEqual(
            extract_image_candidate_urls(html, base_url="https://lg.com/ru/tv/55mrgb"),
            ("https://lg.com/img/photo.png",),
        )

    def test_protocol_relative_url_is_resolved(self):
        html = '<html><body><img src="//cdn.lg.com/photo.webp"></body></html>'
        self.assertEqual(
            extract_image_candidate_urls(html, base_url="https://lg.com/ru/tv"),
            ("https://cdn.lg.com/photo.webp",),
        )

    def test_data_uri_images_are_never_returned(self):
        html = '<html><body><img src="data:image/png;base64,AAAABBBB"></body></html>'
        self.assertEqual(extract_image_candidate_urls(html, base_url="https://lg.com/x"), ())

    def test_duplicate_urls_within_one_page_are_deduplicated(self):
        html = '<html><body><img src="https://lg.com/a.jpg"><img src="https://lg.com/a.jpg"></body></html>'
        self.assertEqual(extract_image_candidate_urls(html, base_url="https://lg.com/x"), ("https://lg.com/a.jpg",))

    def test_result_is_bounded_per_page(self):
        from product_enrichment.research import MAX_IMAGE_CANDIDATES_PER_PAGE

        tags = "".join(f'<img src="https://lg.com/{i}.jpg">' for i in range(10))
        html = f"<html><body>{tags}</body></html>"
        self.assertEqual(len(extract_image_candidate_urls(html, base_url="https://lg.com/x")), MAX_IMAGE_CANDIDATES_PER_PAGE)


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

    def test_media_sink_is_populated_from_og_image_on_accepted_page(self):
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-review"
        image_url = "https://www.lg.com/ru/photos/55mrgb86b6a-hero.jpg"
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": [fake_result(url, title="LG 55MRGB86B6A.ARUG review")]})
        html = f'<html><head><meta property="og:image" content="{image_url}"></head><body>Цвет: черный</body></html>'
        fetch = _FakeFetchPort({url: html})
        media_sink = []
        _run(research_product(_identity(), search_port=search, fetch_port=fetch, media_sink=media_sink))
        self.assertEqual(len(media_sink), 1)
        self.assertEqual(media_sink[0].url, image_url)
        self.assertEqual(media_sink[0].source_type, SOURCE_MANUFACTURER)

    def test_media_sink_omitted_reproduces_exact_prior_behavior(self):
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-specs"
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": [fake_result(url, title="LG 55MRGB86B6A.ARUG specs")]})
        fetch = _FakeFetchPort({url: "Цвет: черный"})
        facts = _run(research_product(_identity(), search_port=search, fetch_port=fetch))
        self.assertTrue(any(f.characteristic_key == "color" for f in facts))

    def test_media_sink_relative_image_url_is_resolved_against_page_url(self):
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-specs"
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": [fake_result(url, title="LG 55MRGB86B6A.ARUG specs")]})
        html = '<html><body><img src="/photos/hero.jpg"></body></html>'
        fetch = _FakeFetchPort({url: html})
        media_sink = []
        _run(research_product(_identity(), search_port=search, fetch_port=fetch, media_sink=media_sink))
        self.assertEqual(len(media_sink), 1)
        self.assertEqual(media_sink[0].url, "https://www.lg.com/photos/hero.jpg")

    def test_media_sink_deduplicates_repeated_image_urls_across_pages(self):
        url_a = "https://www.lg.com/ru/a"
        url_b = "https://www.lg.com/ru/b"
        image_url = "https://www.lg.com/ru/shared.jpg"
        search = FakeSearchProvider(
            {
                "LG 55MRGB86B6A.ARUG": [
                    fake_result(url_a, title="LG 55MRGB86B6A.ARUG page a"),
                    fake_result(url_b, title="LG 55MRGB86B6A.ARUG page b"),
                ]
            }
        )
        html = f'<html><body><img src="{image_url}"></body></html>'
        fetch = _FakeFetchPort({url_a: html, url_b: html})
        media_sink = []
        _run(research_product(_identity(), search_port=search, fetch_port=fetch, media_sink=media_sink))
        self.assertEqual(len(media_sink), 1)

    def test_media_sink_orders_manufacturer_source_before_retail_catalog(self):
        manufacturer_url = "https://www.lg.com/ru/manufacturer-page"
        retail_url = "https://some-random-shop.example/retail-page"
        manufacturer_image = "https://www.lg.com/ru/manufacturer.jpg"
        retail_image = "https://some-random-shop.example/retail.jpg"
        search = FakeSearchProvider(
            {
                "LG 55MRGB86B6A.ARUG": [
                    # Retail result listed FIRST in search results -- ordering in the
                    # final media_sink must still reflect source trust, not discovery order.
                    fake_result(retail_url, title="LG 55MRGB86B6A.ARUG retail listing"),
                    fake_result(manufacturer_url, title="LG 55MRGB86B6A.ARUG manufacturer page"),
                ]
            }
        )
        fetch = _FakeFetchPort(
            {
                retail_url: f'<html><body><img src="{retail_image}">Цвет: черный</body></html>',
                manufacturer_url: f'<html><body><img src="{manufacturer_image}">Цвет: черный</body></html>',
            }
        )
        media_sink = []
        _run(research_product(_identity(), search_port=search, fetch_port=fetch, media_sink=media_sink))
        self.assertEqual(len(media_sink), 2)
        self.assertEqual(media_sink[0].url, manufacturer_image)
        self.assertEqual(media_sink[0].source_type, SOURCE_MANUFACTURER)
        self.assertEqual(media_sink[1].url, retail_image)

    def test_media_sink_never_appends_beyond_run_cap(self):
        from product_enrichment.research import MAX_MEDIA_CANDIDATES_PER_RUN

        results = []
        text_by_url = {}
        for i in range(MAX_MEDIA_CANDIDATES_PER_RUN + 5):
            url = f"https://www.lg.com/ru/page-{i}"
            image_url = f"https://www.lg.com/ru/photo-{i}.jpg"
            results.append(fake_result(url, title="LG 55MRGB86B6A.ARUG page"))
            text_by_url[url] = f'<html><body><img src="{image_url}">Цвет: черный</body></html>'
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": results})
        fetch = _FakeFetchPort(text_by_url)
        media_sink = []
        _run(
            research_product(
                _identity(), search_port=search, fetch_port=fetch, media_sink=media_sink, max_sources=len(results)
            )
        )
        self.assertLessEqual(len(media_sink), MAX_MEDIA_CANDIDATES_PER_RUN)

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
