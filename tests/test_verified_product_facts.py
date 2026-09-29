"""Quality gates run before preview; no live Bitrix calls."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from business_assistant.product_enrichment_bridge import build_enriched_write_request
from product_enrichment.characteristics import (
    bridge_characteristics_to_bitrix, extract_compact_feature_facts,
    merge_facts_into_characteristics, normalize_characteristic_value,
)
from product_enrichment.content import generate_content
from product_enrichment.identity import evidence_matches_identity, resolve_identity
from product_enrichment.models import (
    ContentDraft, EnrichmentResult, NormalizedCharacteristic, ProductIdentityQuery, SourceFact,
)
from product_enrichment.research import research_product


def identity(ean=""):
    return resolve_identity(ProductIdentityQuery(brand="TCL", model="55C6K", ean=ean))


@pytest.mark.parametrize("suffix", ["S", "-UKX1", ".ARUG"])
def test_model_prefix_is_not_exact_identity(suffix):
    assert not evidence_matches_identity(identity(), text="55C6K" + suffix, url="")


@pytest.mark.parametrize("url", ["https://example.com/55C6K.pdf", "https://example.com/55c6k"])
def test_exact_model_url_and_document_are_accepted(url):
    assert evidence_matches_identity(identity(), text="", url=url)


def test_panel_and_backlight_are_independent_and_do_not_match_word_fragments():
    facts = dict(extract_compact_feature_facts("RGB Mini LED HVA Pro panel"))
    assert facts["panel_technology"] == "HVA Pro"
    assert facts["backlight_technology"] == "RGB Mini LED"
    assert "panel_technology" not in dict(extract_compact_feature_facts("innovative native display"))
    assert normalize_characteristic_value("panel_technology", "Mini LED")[0] == ""
    assert normalize_characteristic_value("backlight_technology", "OLED")[0] == ""


def test_probable_is_blocked_in_write_fields_and_stale_generated_content():
    chars = {
        "screen_diagonal_cm": NormalizedCharacteristic(
            key="screen_diagonal_cm", value="139", confidence="probable", bitrix_writable=True),
        "operating_system": NormalizedCharacteristic(
            key="operating_system", value="Google TV", confidence="verified", bitrix_writable=True),
    }
    bridged = bridge_characteristics_to_bitrix(chars)
    assert not bridged["screen_diagonal_cm"].bitrix_writable
    result = EnrichmentResult(identity=identity(), characteristics=chars,
                              content=ContentDraft(short_description="stale 139", detailed_description="stale 139"))
    request = build_enriched_write_request(
        {"sku": "55C6K", "brand": "TCL"}, tenant_id="test", retail_price="54500", enrichment=result)
    assert "screen_diagonal_cm" not in request.characteristics
    assert request.characteristics["operating_system"] == "Google TV"
    assert "139" not in request.short_description + request.detailed_description
    assert "Google TV" in request.detailed_description
    assert "139" not in generate_content(identity(), chars).seo_description


def test_blocked_manufacturer_and_two_domains_cannot_promote_variant_fact():
    fact = SourceFact("usb_count", "USB", "3", "3", "", "https://a.test", "manufacturer",
                      "a.test", "probable", verification_blocker="stock_variant_not_verified")
    chars, _ = merge_facts_into_characteristics((fact, replace(fact, source_domain="b.test")))
    assert chars["usb_count"].confidence == "probable"


class Source:
    def __init__(self, body, snippet=""):
        self.body, self.snippet = body, snippet

    async def search(self, query, max_results=5):
        return [SimpleNamespace(url="https://www.tcl.com/au/en/55c6k",
                                title="TCL 55C6K", snippet=self.snippet)]

    async def fetch_text(self, url):
        return self.body


@pytest.mark.asyncio
@pytest.mark.parametrize("ean,body_diagonal,expected", [
    ("", "138.8 cm", "verified"),
    ("1234567890123", "138.8 cm", "verified"),
    ("1234567890123", '55"', "probable"),
])
async def test_official_exact_model_needs_literal_metric_but_not_mandatory_ean(ean, body_diagonal, expected):
    source = Source(f"<p>EAN: 1234567890123</p><p>Диагональ экрана: {body_diagonal}</p>"
                    "<p>Операционная система: Google TV</p>")
    facts = await research_product(identity(ean), search_port=source, fetch_port=source)
    chars, _ = merge_facts_into_characteristics(facts)
    assert chars["screen_diagonal_cm"].confidence == expected
    assert chars["operating_system"].confidence == "verified"


@pytest.mark.asyncio
async def test_search_snippet_cannot_override_fetched_page_or_create_specs():
    source = Source("<p>Операционная система: Google TV</p>", "Tizen, Wi-Fi, Mini LED")
    facts = await research_product(identity(), search_port=source, fetch_port=source)
    chars, _ = merge_facts_into_characteristics(facts)
    assert chars["operating_system"].value == "Google TV"
    assert "wifi_support" not in chars
    assert "backlight_technology" not in chars


def test_explicit_supplier_content_and_fields_keep_priority():
    result = EnrichmentResult(identity=identity(), characteristics={
        "operating_system": NormalizedCharacteristic(
            key="operating_system", value="Google TV", confidence="verified", bitrix_writable=True)})
    request = build_enriched_write_request(
        {"sku": "55C6K", "brand": "TCL", "short_description": "Supplier description",
         "characteristics": {"operating_system": "Explicit supplier value"}},
        tenant_id="test", retail_price="54500", enrichment=result)
    assert request.short_description == "Supplier description"
    assert request.characteristics["operating_system"] == "Explicit supplier value"


@pytest.mark.asyncio
async def test_cached_probable_content_is_regenerated_without_research():
    from product_enrichment.cache import EnrichmentCache
    from product_enrichment.orchestrator import enrich_product

    cache = EnrichmentCache()
    chars = {"color": NormalizedCharacteristic(key="color", value="BLack", confidence="probable")}
    cache.put(tenant_id="test", identity_key=identity().identity_key,
              result=EnrichmentResult(identity=identity(), characteristics=chars,
                                      content=ContentDraft(short_description="BLack", detailed_description="BLack")))
    result = await enrich_product(tenant_id="test", query=ProductIdentityQuery(brand="TCL", model="55C6K"),
                                  cache=cache)
    assert result.cache_hit
    assert "BLack" not in result.content.short_description + result.content.detailed_description
