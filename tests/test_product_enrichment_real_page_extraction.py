"""Production defect closure: successfully fetched product pages produced
ZERO characteristics, ZERO media and a fact-free ("prepared") description.

Every HTML fixture below is a REDUCED COPY of the markup shapes real,
production-fetched catalog pages actually use (a minified body with no line
breaks; label and value in SEPARATE elements -- ``<th>``/``<td>``,
``<dt>``/``<dd>``, nested ``<div>``/``<span>`` -- character entities such
as ``55&quot;``/``120&nbsp;Гц``; lazy-loaded product photos in
``data-src``/``srcset`` while ``src`` holds a placeholder; a site logo,
an icon sprite and an analytics beacon as the first ``<img>`` tags; and
cross-sell/navigation blocks whose text contains characteristic words).

Before the fix, ``extract_spec_lines`` only understood ``label: value`` on
a single PLAIN-TEXT line, so none of these pages yielded a single
characteristic, ``generate_content`` had no facts to compose a description
from, and the bounded image-candidate budget was consumed by logos/icon
sprites/beacons.

Nothing here is brand- or model-specific: the same fixtures are used with
two unrelated brands/models, and no expected value is hardcoded anywhere
except as the literal text present in the fixture markup.
"""

from __future__ import annotations

import asyncio
import io
import unittest

from product_enrichment.characteristics import (
    extract_spec_lines,
    match_canonical_key,
    normalize_characteristic_value,
)
from product_enrichment.identity import resolve_identity
from product_enrichment.media_fetch import FakeImageFetcher
from product_enrichment.models import (
    CONFIDENCE_VERIFIED,
    MediaResult,
    ProductIdentityQuery,
)
from product_enrichment.orchestrator import enrich_product
from product_enrichment.preview import enrichment_preview_dict, format_enrichment_preview_text
from product_enrichment.research import extract_image_candidate_urls, research_product
from tools.search.fake_provider import FakeSearchProvider, fake_result


def _run(coro):
    return asyncio.run(coro)


def _png_bytes(width: int, height: int, color=(24, 32, 48)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


# --- realistic page fixtures -------------------------------------------------

# Shape 1: nested div/span feature rows (minified, no colon, no newline)
# plus lazy-loaded gallery images and the usual junk assets first.
RETAILER_PAGE_HTML = (
    "<html><head>"
    '<meta property="og:image" content="https://shop-one.example/img/products/hero-1000.jpg">'
    "</head><body>"
    '<img src="https://beacon.example/watch/12345" style="position:absolute;left:-9999px" alt="">'
    '<img class="logo" src="/theme/img/logo.svg" alt="Shop One">'
    '<img class="svg-icon" src="/theme/svg/icon.sprite.svg?v=2#telegram" width="24" height="24">'
    '<h1>{name}</h1>'
    '<img class="product-gallery__image lazy" src="/theme/img/placeholder.png"'
    ' data-src="/img/products/gallery-2.jpg" data-srcset="/img/products/gallery-3.jpg 2x">'
    '<div class="product-features">'
    '<div class="product-feature"><div class="product-feature__name-box">'
    '<span class="product-feature__name">Диагональ экрана</span></div>'
    '<div class="product-feature__value">55&quot;</div></div>'
    '<div class="product-feature"><div class="product-feature__name-box">'
    '<span class="product-feature__name">Частота обновления</span></div>'
    '<div class="product-feature__value">120&nbsp;Гц</div></div>'
    '<div class="product-feature"><div class="product-feature__name-box">'
    '<span class="product-feature__name">Цвет</span></div>'
    '<div class="product-feature__value">черный</div></div>'
    "</div>"
    '<nav class="catalog-menu"><a href="/cables/hdmi/">HDMI-кабели</a>'
    '<a href="/sockets/">Ethernet-розетки, вставки, корпуса</a></nav>'
    '<div class="cross-sell">Телевизор ACME 65" OTHER-MODEL-65 (Цвет: Silver) 199 990 руб.</div>'
    "</body></html>"
)

# Shape 2: a spec TABLE plus a definition list, different domain.
DISTRIBUTOR_PAGE_HTML = (
    "<html><head>"
    '<meta itemprop="image" content="https://shop-two.example/upload/photo-970.jpg">'
    "</head><body><h1>{name}</h1>"
    '<table class="specs"><tbody>'
    "<tr><th>Диагональ экрана</th><td>55&quot;</td></tr>"
    "<tr><th>Разрешение экрана</th><td>3840x2160</td></tr>"
    "<tr><th>Тип матрицы</th><td>IPS</td></tr>"
    "</tbody></table>"
    '<dl class="chars">'
    "<dt>Операционная система</dt><dd>webOS 24</dd>"
    "<dt>Мощность звука</dt><dd>20 Вт</dd>"
    "<dt>Количество HDMI</dt><dd>4</dd>"
    "</dl></body></html>"
)

# A page that genuinely has no recognizable characteristics/images: the
# fail-closed path must stay fail-closed.
CONTENT_FREE_PAGE_HTML = "<html><body><h1>{name}</h1><p>Товар временно недоступен.</p></body></html>"


class _FixtureFetchPort:
    def __init__(self, html_by_url: dict[str, str]):
        self.html_by_url = dict(html_by_url)
        self.requested: list[str] = []

    async def fetch_text(self, url: str) -> str:
        self.requested.append(url)
        return self.html_by_url.get(url, "")


class RealPageSpecExtractionTests(unittest.TestCase):
    """The extraction layer itself, on real-world markup shapes."""

    def test_structural_div_span_feature_rows_are_extracted(self):
        pairs = dict(extract_spec_lines(RETAILER_PAGE_HTML.format(name="ACME MODEL-55")))
        self.assertEqual(pairs.get("Диагональ экрана"), '55"')
        self.assertEqual(pairs.get("Частота обновления"), "120 Гц")
        self.assertEqual(pairs.get("Цвет"), "черный")

    def test_table_rows_and_definition_lists_are_extracted(self):
        pairs = dict(extract_spec_lines(DISTRIBUTOR_PAGE_HTML.format(name="ACME MODEL-55")))
        self.assertEqual(pairs.get("Разрешение экрана"), "3840x2160")
        self.assertEqual(pairs.get("Тип матрицы"), "IPS")
        self.assertEqual(pairs.get("Операционная система"), "webOS 24")
        self.assertEqual(pairs.get("Мощность звука"), "20 Вт")
        self.assertEqual(pairs.get("Количество HDMI"), "4")

    def test_character_entities_and_nbsp_are_decoded_into_readable_values(self):
        pairs = dict(extract_spec_lines(RETAILER_PAGE_HTML.format(name="ACME MODEL-55")))
        self.assertNotIn("&quot;", pairs.get("Диагональ экрана", ""))
        self.assertNotIn("\u00a0", pairs.get("Частота обновления", ""))

    def test_navigation_link_text_is_never_a_specification(self):
        pairs = list(extract_spec_lines(RETAILER_PAGE_HTML.format(name="ACME MODEL-55")))
        for label, value in pairs:
            self.assertNotIn("кабели", value.casefold())
            self.assertNotIn("розетки", value.casefold())

    def test_cross_sell_text_containing_a_characteristic_word_is_not_a_label(self):
        pairs = list(extract_spec_lines(RETAILER_PAGE_HTML.format(name="ACME MODEL-55")))
        for label, value in pairs:
            if match_canonical_key(label) == "color":
                self.assertEqual(value, "черный")

    def test_long_marketing_sentence_containing_a_characteristic_word_is_rejected(self):
        self.assertIsNone(
            match_canonical_key('Телевизор ACME 65" OTHER-MODEL-65 (Цвет')
        )

    def test_declared_unit_synonym_is_not_duplicated_in_the_normalized_value(self):
        self.assertEqual(normalize_characteristic_value("refresh_rate_hz", "120 Гц"), ("120", "Hz"))
        self.assertEqual(normalize_characteristic_value("audio_power_w", "20 Вт"), ("20", "W"))

    def test_value_carrying_extra_qualifiers_is_kept_verbatim(self):
        self.assertEqual(
            normalize_characteristic_value("refresh_rate_hz", "до 120 Гц (VRR)"),
            ("до 120 Гц (VRR)", "Hz"),
        )

    def test_page_without_recognizable_specs_yields_nothing(self):
        self.assertEqual(list(extract_spec_lines(CONTENT_FREE_PAGE_HTML.format(name="ACME X"))), [])


class RealPageImageCandidateTests(unittest.TestCase):
    def test_lazy_loaded_product_photos_are_discovered(self):
        urls = extract_image_candidate_urls(
            RETAILER_PAGE_HTML.format(name="ACME MODEL-55"), base_url="https://shop-one.example/p/acme"
        )
        self.assertIn("https://shop-one.example/img/products/gallery-2.jpg", urls)

    def test_page_declared_canonical_image_comes_first(self):
        urls = extract_image_candidate_urls(
            RETAILER_PAGE_HTML.format(name="ACME MODEL-55"), base_url="https://shop-one.example/p/acme"
        )
        self.assertEqual(urls[0], "https://shop-one.example/img/products/hero-1000.jpg")

    def test_itemprop_image_metadata_is_discovered(self):
        urls = extract_image_candidate_urls(
            DISTRIBUTOR_PAGE_HTML.format(name="ACME MODEL-55"), base_url="https://shop-two.example/p/acme"
        )
        self.assertEqual(urls, ("https://shop-two.example/upload/photo-970.jpg",))

    def test_logos_icon_sprites_and_analytics_beacons_are_never_candidates(self):
        urls = extract_image_candidate_urls(
            RETAILER_PAGE_HTML.format(name="ACME MODEL-55"), base_url="https://shop-one.example/p/acme"
        )
        joined = " ".join(urls).casefold()
        for junk in ("logo", "sprite", "icon", "placeholder", "beacon.example"):
            self.assertNotIn(junk, joined)


class RealPageEnrichmentPipelineTests(unittest.TestCase):
    """End-to-end: realistic search results + realistic page HTML must
    produce verified characteristics, real media through the existing safe
    pipeline, and a fact-based description."""

    BRAND = "ACME"
    MODEL = "MODEL-55X"
    RETAILER_URL = "https://shop-one.example/catalog/acme-model-55x"
    DISTRIBUTOR_URL = "https://shop-two.example/product/acme-model-55x"
    HERO_URL = "https://shop-one.example/img/products/hero-1000.jpg"
    GALLERY_URL = "https://shop-one.example/img/products/gallery-2.jpg"
    DISTRIBUTOR_IMAGE_URL = "https://shop-two.example/upload/photo-970.jpg"

    def _query(self):
        return ProductIdentityQuery(
            brand=self.BRAND,
            model=self.MODEL,
            article=self.MODEL,
            ean="4001234567890",
            category="CE",
            subcategory="TV",
        )

    def _search(self):
        name = f"{self.BRAND} {self.MODEL}"
        return FakeSearchProvider(
            {
                f"{self.BRAND} {self.MODEL} характеристики specifications": [
                    fake_result(self.RETAILER_URL, title=name, snippet=f"Купить {name}"),
                    fake_result(self.DISTRIBUTOR_URL, title=name, snippet=f"{name} характеристики"),
                ]
            }
        )

    def _fetch(self):
        name = f"{self.BRAND} {self.MODEL}"
        return _FixtureFetchPort(
            {
                self.RETAILER_URL: RETAILER_PAGE_HTML.format(name=name),
                self.DISTRIBUTOR_URL: DISTRIBUTOR_PAGE_HTML.format(name=name),
            }
        )

    def _image_fetcher(self):
        return FakeImageFetcher(
            {
                self.HERO_URL: _png_bytes(1400, 900),
                self.GALLERY_URL: _png_bytes(900, 900, color=(60, 70, 80)),
                "https://shop-one.example/img/products/gallery-3.jpg": _png_bytes(800, 800, color=(90, 20, 20)),
                self.DISTRIBUTOR_IMAGE_URL: _png_bytes(970, 601, color=(10, 90, 40)),
            }
        )

    def _enrich(self, *, media_fetcher=None, fetch=None):
        return _run(
            enrich_product(
                tenant_id="tenant-1",
                query=self._query(),
                search_port=self._search(),
                fetch_port=fetch or self._fetch(),
                media_fetcher=media_fetcher if media_fetcher is not None else self._image_fetcher(),
            )
        )

    def test_research_produces_facts_from_real_markup(self):
        media: list = []
        facts = _run(
            research_product(
                resolve_identity(self._query()),
                search_port=self._search(),
                fetch_port=self._fetch(),
                media_sink=media,
            )
        )
        self.assertTrue(facts, "realistic product pages must yield source facts")
        self.assertTrue(media, "realistic product pages must yield media candidates")

    def test_normalized_characteristics_are_produced(self):
        result = self._enrich()
        self.assertGreaterEqual(len(result.characteristics), 5)
        self.assertEqual(result.characteristics["screen_resolution"].value, "3840x2160")
        self.assertEqual(result.characteristics["refresh_rate_hz"].value, "120")
        self.assertEqual(result.characteristics["refresh_rate_hz"].unit, "Hz")
        self.assertEqual(result.characteristics["color"].value, "черный")

    def test_independent_sources_agreeing_yield_a_verified_characteristic(self):
        result = self._enrich()
        diagonal = result.characteristics["screen_diagonal_cm"]
        self.assertEqual(diagonal.value, "139.7")
        self.assertEqual(diagonal.unit, "cm")
        self.assertEqual(diagonal.confidence, CONFIDENCE_VERIFIED)
        self.assertGreaterEqual(len({f.source_domain for f in diagonal.supporting_facts}), 2)

    def test_no_characteristic_value_is_invented(self):
        result = self._enrich()
        for characteristic in result.characteristics.values():
            self.assertTrue(characteristic.supporting_facts)
            for fact in characteristic.supporting_facts:
                self.assertIn(fact.source_url, (self.RETAILER_URL, self.DISTRIBUTOR_URL))

    def test_media_is_acquired_through_the_existing_safe_pipeline(self):
        fetcher = self._image_fetcher()
        result = self._enrich(media_fetcher=fetcher)
        self.assertEqual(result.media.status, MediaResult.STATUS_READY)
        roles = {asset.role for asset in result.media.assets}
        self.assertIn("preview", roles)
        self.assertIn("detail", roles)
        for asset in result.media.assets:
            self.assertTrue(asset.base64_content, "Bitrix receives file bytes, never a hotlink")
            self.assertTrue(asset.content_hash)
        # Every downloaded URL was literally present in a fetched page.
        for url in fetcher.requested_urls:
            self.assertIn(
                url,
                (self.HERO_URL, self.GALLERY_URL, "https://shop-one.example/img/products/gallery-3.jpg", self.DISTRIBUTOR_IMAGE_URL),
            )

    def test_main_image_master_is_a_product_photo_not_a_site_asset(self):
        result = self._enrich()
        preview = next(asset for asset in result.media.assets if asset.role == "preview")
        self.assertEqual(preview.source_url, self.HERO_URL)

    def test_detailed_description_is_composed_from_verified_facts(self):
        result = self._enrich()
        detailed = result.content.detailed_description
        self.assertIn("Основные характеристики", detailed)
        self.assertIn("139.7", detailed)
        self.assertIn("120", detailed)
        self.assertTrue(result.content.facts_used)
        # Every number in the description is backed by a characteristic.
        for key in result.content.facts_used:
            self.assertIn(key, result.characteristics)

    def test_preview_shows_the_generated_description_not_just_a_status_word(self):
        result = self._enrich()
        data = enrichment_preview_dict(result)
        text = format_enrichment_preview_text(result)
        self.assertEqual(data["content"]["detailed_description_status"], "prepared")
        self.assertIn("Основные характеристики", data["content"]["detailed_description"])
        self.assertIn("Основные характеристики", text)
        self.assertNotIn("Подробное описание: prepared", text)

    def test_preview_reports_ready_characteristics_and_media(self):
        result = self._enrich()
        data = enrichment_preview_dict(result)
        self.assertGreater(data["characteristics"]["count"], 0)
        self.assertGreaterEqual(data["characteristics"]["verified_count"], 1)
        self.assertTrue(data["media"]["main_image_prepared"])
        self.assertIn("characteristics", data["ready_to_write"])
        self.assertIn("media", data["ready_to_write"])
        self.assertNotIn("characteristics", data["missing_source_data"])
        self.assertNotIn("media", data["missing_source_data"])

    def test_pipeline_still_fails_closed_when_pages_carry_no_product_data(self):
        name = f"{self.BRAND} {self.MODEL}"
        empty_fetch = _FixtureFetchPort(
            {
                self.RETAILER_URL: CONTENT_FREE_PAGE_HTML.format(name=name),
                self.DISTRIBUTOR_URL: CONTENT_FREE_PAGE_HTML.format(name=name),
            }
        )
        result = self._enrich(fetch=empty_fetch, media_fetcher=self._image_fetcher())
        self.assertEqual(result.characteristics, {})
        self.assertEqual(result.media.status, MediaResult.STATUS_UNRESOLVED)
        data = enrichment_preview_dict(result)
        self.assertIn("characteristics", data["missing_source_data"])
        self.assertIn("media", data["missing_source_data"])
        self.assertNotIn("Основные характеристики", result.content.detailed_description)

    def test_media_rejection_reasons_are_surfaced_when_nothing_could_be_prepared(self):
        result = self._enrich(media_fetcher=FakeImageFetcher({}))
        self.assertEqual(result.media.status, MediaResult.STATUS_UNRESOLVED)
        data = enrichment_preview_dict(result)
        self.assertTrue(data["media"]["rejected_reasons"])
        self.assertIn("Изображения отклонены", format_enrichment_preview_text(result))

    def test_extraction_is_not_specific_to_one_brand_or_model(self):
        other_brand, other_model = "Zenith", "ZN-77QD"
        name = f"{other_brand} {other_model}"
        query = ProductIdentityQuery(brand=other_brand, model=other_model, article=other_model)
        search = FakeSearchProvider(
            {
                f"{other_brand} {other_model} характеристики specifications": [
                    fake_result("https://shop-two.example/product/zn-77qd", title=name)
                ]
            }
        )
        fetch = _FixtureFetchPort(
            {"https://shop-two.example/product/zn-77qd": DISTRIBUTOR_PAGE_HTML.format(name=name)}
        )
        result = _run(
            enrich_product(tenant_id="tenant-1", query=query, search_port=search, fetch_port=fetch)
        )
        self.assertEqual(result.characteristics["screen_resolution"].value, "3840x2160")
        self.assertIn("3840x2160", result.content.detailed_description)


class DegradedResultIsNotCachedTests(unittest.TestCase):
    """A run whose research produced nothing is degraded, not
    deterministic: caching it would pin the failure onto every later
    request for the same product in this process."""

    def _query(self):
        return ProductIdentityQuery(brand="ACME", model="MODEL-55X", article="MODEL-55X")

    def test_zero_fact_result_is_not_cached(self):
        from product_enrichment.cache import EnrichmentCache

        cache = EnrichmentCache()
        url = "https://shop-one.example/catalog/acme-model-55x"
        search = FakeSearchProvider(
            {"ACME MODEL-55X характеристики specifications": [fake_result(url, title="ACME MODEL-55X")]}
        )
        result = _run(
            enrich_product(
                tenant_id="t",
                query=self._query(),
                search_port=search,
                fetch_port=_FixtureFetchPort({url: CONTENT_FREE_PAGE_HTML.format(name="ACME MODEL-55X")}),
                cache=cache,
            )
        )
        self.assertEqual(result.facts, ())
        self.assertEqual(len(cache), 0)

    def test_successful_result_is_still_cached(self):
        from product_enrichment.cache import EnrichmentCache

        cache = EnrichmentCache()
        url = "https://shop-two.example/product/acme-model-55x"
        search = FakeSearchProvider(
            {"ACME MODEL-55X характеристики specifications": [fake_result(url, title="ACME MODEL-55X")]}
        )
        fetch = _FixtureFetchPort({url: DISTRIBUTOR_PAGE_HTML.format(name="ACME MODEL-55X")})
        first = _run(
            enrich_product(tenant_id="t", query=self._query(), search_port=search, fetch_port=fetch, cache=cache)
        )
        self.assertTrue(first.facts)
        self.assertEqual(len(cache), 1)
        second = _run(
            enrich_product(tenant_id="t", query=self._query(), search_port=search, fetch_port=fetch, cache=cache)
        )
        self.assertTrue(second.cache_hit)


if __name__ == "__main__":
    unittest.main()
