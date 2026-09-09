"""Product enrichment pipeline: top-level orchestration (``enrich_product``)
-- identity -> research -> characteristics -> content -> media, with
caching (requirement 13) and observability (requirement 14), and
fail-safe degradation when research/media capabilities are absent
(requirement 15)."""

from __future__ import annotations

import io
import unittest

from PIL import Image

from product_enrichment.cache import EnrichmentCache
from product_enrichment.identity import IdentityConflictError
from product_enrichment.media_fetch import FakeImageFetcher
from product_enrichment.models import (
    CONFIDENCE_VERIFIED,
    MediaCandidateInput,
    ProductIdentityQuery,
    SOURCE_MANUFACTURER,
    SourceFact,
)
from product_enrichment.observability import EnrichmentObserver
from product_enrichment.orchestrator import enrich_product
from tools.search.fake_provider import FakeSearchProvider, fake_result


def _png(w: int = 400, h: int = 400) -> bytes:
    img = Image.new("RGB", (w, h), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class _FakeFetchPort:
    def __init__(self, text_by_url: dict[str, str]):
        self.text_by_url = text_by_url

    async def fetch_text(self, url: str) -> str:
        return self.text_by_url.get(url, "")


def _query():
    return ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG", ean="8806096824788", category="CE", subcategory="TV")


class EnrichProductTests(unittest.TestCase):
    def test_invalid_identity_raises_and_never_produces_a_partial_result(self):
        with self.assertRaises(IdentityConflictError):
            _run(enrich_product(tenant_id="t1", query=ProductIdentityQuery(brand="")))

    def test_no_search_or_fetch_port_skips_research_without_hallucinating(self):
        result = _run(enrich_product(tenant_id="t1", query=_query()))
        self.assertFalse(result.research_available)
        self.assertEqual(result.characteristics, {})
        self.assertEqual(result.facts, ())
        # Identity/content are still always produced (module docstring
        # requirement 15's "preview known fields + mark missing").
        self.assertEqual(result.identity.brand, "LG")
        self.assertTrue(result.content.short_description)

    def test_extra_facts_alone_still_produce_characteristics_without_research(self):
        extra = (
            SourceFact(
                characteristic_key="color",
                raw_label="color",
                raw_value="black",
                normalized_value="black",
                unit="",
                source_url="https://lg.com/x",
                source_type=SOURCE_MANUFACTURER,
                source_domain="lg.com",
                confidence=CONFIDENCE_VERIFIED,
            ),
        )
        result = _run(enrich_product(tenant_id="t1", query=_query(), extra_facts=extra))
        self.assertIn("color", result.characteristics)

    def test_full_pipeline_with_research_and_media(self):
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-specs"
        image_url = "https://www.lg.com/ru/55MRGB86B6A.ARUG.png"
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": [fake_result(url, title="LG 55MRGB86B6A.ARUG specs")]})
        fetch = _FakeFetchPort({url: "Диагональ экрана: 139 см\nЦвет: черный"})
        media_fetcher = FakeImageFetcher({image_url: _png()})
        observer = EnrichmentObserver()

        result = _run(
            enrich_product(
                tenant_id="t1",
                query=_query(),
                search_port=search,
                fetch_port=fetch,
                media_fetcher=media_fetcher,
                media_candidates=(MediaCandidateInput(url=image_url, source_type=SOURCE_MANUFACTURER),),
                observer=observer,
            )
        )
        self.assertTrue(result.research_available)
        self.assertIn("screen_diagonal_cm", result.characteristics)
        self.assertIn("color", result.characteristics)
        self.assertTrue(result.content.short_description)
        self.assertTrue(any(a.role == "preview" for a in result.media.assets))
        self.assertFalse(result.cache_hit)
        self.assertIn("product_identity_resolved", observer.stages())
        self.assertIn("enrichment_preview_ready", observer.stages())

    def test_media_candidates_auto_discovered_from_research_without_explicit_candidates(self):
        """Requirement 7 (the "MEDIA GAP"): with NO explicit media_candidates
        supplied at all, an image URL discovered in the SAME
        identity-verified, non-conflicting page research already fetched
        for characteristics must still reach GovernedImageFetcher /
        MediaAcquisitionService and produce a real processed asset."""
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-review"
        image_url = "https://www.lg.com/ru/photos/hero.png"
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": [fake_result(url, title="LG 55MRGB86B6A.ARUG review")]})
        html = f'<html><head><meta property="og:image" content="{image_url}"></head><body>Цвет: черный</body></html>'
        fetch = _FakeFetchPort({url: html})
        media_fetcher = FakeImageFetcher({image_url: _png()})

        result = _run(
            enrich_product(
                tenant_id="t1",
                query=_query(),
                search_port=search,
                fetch_port=fetch,
                media_fetcher=media_fetcher,
                # Deliberately NOT passing media_candidates.
            )
        )
        self.assertTrue(any(a.role == "preview" for a in result.media.assets))
        self.assertEqual(result.media.status, result.media.STATUS_READY)
        preview_asset = next(a for a in result.media.assets if a.role == "preview")
        # The external URL is retained only as provenance -- never the
        # thing actually handed downstream (that's base64_content).
        self.assertEqual(preview_asset.source_url, image_url)
        self.assertTrue(preview_asset.base64_content)
        self.assertNotIn(image_url, preview_asset.base64_content)

    def test_explicit_media_candidates_take_priority_over_discovered_ones(self):
        research_url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-review"
        discovered_image = "https://www.lg.com/ru/photos/discovered.png"
        explicit_image = "https://www.lg.com/ru/photos/explicit.png"
        search = FakeSearchProvider(
            {"LG 55MRGB86B6A.ARUG": [fake_result(research_url, title="LG 55MRGB86B6A.ARUG review")]}
        )
        html = f'<html><head><meta property="og:image" content="{discovered_image}"></head><body>Цвет: черный</body></html>'
        fetch = _FakeFetchPort({research_url: html})
        media_fetcher = FakeImageFetcher({discovered_image: _png(500, 500), explicit_image: _png(300, 300)})

        result = _run(
            enrich_product(
                tenant_id="t1",
                query=_query(),
                search_port=search,
                fetch_port=fetch,
                media_fetcher=media_fetcher,
                media_candidates=(MediaCandidateInput(url=explicit_image, source_type=SOURCE_MANUFACTURER),),
            )
        )
        preview_asset = next(a for a in result.media.assets if a.role == "preview")
        # The explicit candidate is master (accepted[0]); the discovered
        # one is still kept, just ordered behind it (gallery).
        self.assertEqual(preview_asset.source_url, explicit_image)
        self.assertTrue(any(a.source_url == discovered_image for a in result.media.assets))

    def test_no_media_fetcher_configured_never_downloads_discovered_candidates(self):
        """No hotlink/download must ever be attempted when the caller has
        not wired a media_fetcher at all -- discovery alone must never
        cause an implicit network call."""
        research_url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-review"
        discovered_image = "https://www.lg.com/ru/photos/discovered.png"
        search = FakeSearchProvider(
            {"LG 55MRGB86B6A.ARUG": [fake_result(research_url, title="LG 55MRGB86B6A.ARUG review")]}
        )
        html = f'<html><head><meta property="og:image" content="{discovered_image}"></head><body>Цвет: черный</body></html>'
        fetch = _FakeFetchPort({research_url: html})

        result = _run(
            enrich_product(
                tenant_id="t1",
                query=_query(),
                search_port=search,
                fetch_port=fetch,
                # No media_fetcher at all.
            )
        )
        from product_enrichment.models import MediaResult

        self.assertEqual(result.media, MediaResult())

    def test_research_exception_degrades_gracefully_never_raises(self):
        class BrokenSearch:
            async def search(self, query, max_results=5):
                raise RuntimeError("boom")

        result = _run(enrich_product(tenant_id="t1", query=_query(), search_port=BrokenSearch(), fetch_port=_FakeFetchPort({})))
        self.assertEqual(result.facts, ())
        self.assertEqual(result.characteristics, {})

    def test_media_exception_degrades_to_unresolved_never_raises(self):
        class BrokenFetcher:
            async def fetch_bytes(self, url):
                raise RuntimeError("boom")

        from product_enrichment.models import MediaResult

        result = _run(
            enrich_product(
                tenant_id="t1",
                query=_query(),
                media_fetcher=BrokenFetcher(),
                media_candidates=(MediaCandidateInput(url="https://lg.com/x.png"),),
            )
        )
        self.assertEqual(result.media.status, MediaResult.STATUS_UNRESOLVED)

    def test_repeated_call_with_cache_reuses_result_and_skips_research(self):
        url = "https://www.lg.com/ru/55MRGB86B6A.ARUG-specs"
        search = FakeSearchProvider({"LG 55MRGB86B6A.ARUG": [fake_result(url, title="LG 55MRGB86B6A.ARUG specs")]})
        fetch = _FakeFetchPort({url: "Цвет: черный"})
        cache = EnrichmentCache()

        first = _run(enrich_product(tenant_id="t1", query=_query(), search_port=search, fetch_port=fetch, cache=cache))
        self.assertFalse(first.cache_hit)
        self.assertEqual(len(search.queries), 1)

        second = _run(enrich_product(tenant_id="t1", query=_query(), search_port=search, fetch_port=fetch, cache=cache))
        self.assertTrue(second.cache_hit)
        # No additional search call was made -- cache was reused.
        self.assertEqual(len(search.queries), 1)
        self.assertEqual(second.characteristics, first.characteristics)

    def test_cache_is_scoped_per_tenant(self):
        cache = EnrichmentCache()
        result_t1 = _run(enrich_product(tenant_id="tenant-1", query=_query(), cache=cache))
        result_t2 = _run(enrich_product(tenant_id="tenant-2", query=_query(), cache=cache))
        self.assertFalse(result_t1.cache_hit)
        self.assertFalse(result_t2.cache_hit)

    def test_identity_failure_emits_failed_observability_stage(self):
        observer = EnrichmentObserver()
        with self.assertRaises(IdentityConflictError):
            _run(enrich_product(tenant_id="t1", query=ProductIdentityQuery(brand=""), observer=observer))
        self.assertIn("product_identity_failed", observer.stages())
        self.assertIn("enrichment_failed", observer.stages())

    def test_observer_never_logs_base64_media_content(self):
        image_url = "https://www.lg.com/ru/photo.png"
        media_fetcher = FakeImageFetcher({image_url: _png()})
        observer = EnrichmentObserver()
        _run(
            enrich_product(
                tenant_id="t1",
                query=_query(),
                media_fetcher=media_fetcher,
                media_candidates=(MediaCandidateInput(url=image_url),),
                observer=observer,
            )
        )
        for event in observer.events:
            for key in event:
                self.assertNotIn("base64", key.casefold())


if __name__ == "__main__":
    unittest.main()
