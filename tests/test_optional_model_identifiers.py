"""Exact official model evidence works with and without a supplied suffix/EAN."""
from types import SimpleNamespace

import pytest

from business_assistant.product_enrichment_bridge import build_identity_query_from_fields, build_enriched_write_request
from product_enrichment.characteristics import merge_facts_into_characteristics, bridge_characteristics_to_bitrix
from product_enrichment.identity import resolve_identity
from product_enrichment.models import EnrichmentResult, ProductIdentityQuery
from product_enrichment.research import research_product


class Pages:
    def __init__(self, pages):
        self.pages = pages

    async def search(self, query, max_results=5):
        return [SimpleNamespace(url=url, title=title, snippet="") for url, title, body in self.pages]

    async def fetch_text(self, url):
        return next(body for u, _, body in self.pages if u == url)


async def researched(brand, model, pages, ean=""):
    identity = resolve_identity(ProductIdentityQuery(brand=brand, model=model, ean=ean))
    source = Pages(pages)
    facts = await research_product(identity, search_port=source, fetch_port=source)
    chars, conflicts = merge_facts_into_characteristics(facts)
    return identity, bridge_characteristics_to_bitrix(chars), conflicts


@pytest.mark.asyncio
@pytest.mark.parametrize("brand,model,url,diagonal", [
    ("LG", "32LQ63006LA.ARUG", "https://www.lg.com/support/32LQ63006LA.ARUG", "80 cm"),
    ("TCL", "55C6K", "https://www.tcl.com/tr/tr/tvs/55c6k", "138.8 cm"),
])
async def test_official_exact_model_without_ean_reaches_fields_and_description(brand, model, url, diagonal):
    fields = {"brand": brand, "sku": model}
    assert build_identity_query_from_fields(fields).model == model
    identity, chars, conflicts = await researched(
        brand, model, [(url, model, f"<p>Диагональ экрана: {diagonal}</p>")])
    assert identity.model == model
    assert not conflicts
    assert chars["screen_diagonal_cm"].confidence == "verified"
    request = build_enriched_write_request(
        fields, tenant_id="test", retail_price="54500",
        enrichment=EnrichmentResult(identity=identity, characteristics=chars))
    assert request.characteristics["screen_diagonal_cm"] == diagonal.split()[0]
    assert diagonal.split()[0] in request.detailed_description


@pytest.mark.asyncio
@pytest.mark.parametrize("source_model", ["32LQ63006LA", "32LQ63006LA.AEU", "32LQ63006LA.ARUGX"])
async def test_supplied_suffix_is_never_stripped_to_accept_other_variant(source_model):
    _, chars, _ = await researched("LG", "32LQ63006LA.ARUG", [
        (f"https://www.lg.com/tv/{source_model}", source_model, "<p>Диагональ экрана: 80 cm</p>")])
    assert not chars


@pytest.mark.asyncio
async def test_official_search_title_alone_does_not_verify_measurement():
    _, chars, _ = await researched("TCL", "55C6K", [
        ("https://www.tcl.com/global/tvs", "55C6K", "<p>Диагональ экрана: 138.8 cm</p>")])
    assert chars["screen_diagonal_cm"].confidence == "probable"


@pytest.mark.asyncio
async def test_retail_model_only_does_not_gain_manufacturer_trust():
    _, chars, _ = await researched("TCL", "55C6K", [
        ("https://retailer.example/55c6k", "55C6K", "<p>Диагональ экрана: 138.8 cm</p>")])
    assert chars["screen_diagonal_cm"].confidence == "probable"


@pytest.mark.asyncio
async def test_conflicting_official_model_specs_still_stop_that_characteristic():
    _, chars, conflicts = await researched("TCL", "55C6K", [
        ("https://www.tcl.com/tr/tr/tvs/55c6k", "55C6K", "<p>Диагональ экрана: 138.8 cm</p>"),
        ("https://www.tcl.com/au/en/tvs/55c6k", "55C6K", "<p>Диагональ экрана: 139.7 cm</p>")])
    assert "screen_diagonal_cm" not in chars
    assert any(c.code == "characteristic_conflict_screen_diagonal_cm" for c in conflicts)


@pytest.mark.asyncio
async def test_given_ean_conflict_is_not_overridden_by_official_model_match():
    _, chars, _ = await researched("TCL", "55C6K", [
        ("https://www.tcl.com/tr/tr/tvs/55c6k", "55C6K",
         "<p>EAN: 1234567890123</p><p>Диагональ экрана: 138.8 cm</p>")], ean="9999999999999")
    assert chars["screen_diagonal_cm"].confidence == "probable"
    assert chars["screen_diagonal_cm"].supporting_facts[0].verification_blocker == "ean_mismatch"


@pytest.mark.asyncio
async def test_market_url_never_invents_country_of_manufacture():
    _, chars, _ = await researched("TCL", "55C6K", [
        ("https://www.tcl.com/tr/tr/tvs/55c6k", "55C6K", "<p>Диагональ экрана: 138.8 cm</p>")])
    assert "country_of_origin" not in chars
