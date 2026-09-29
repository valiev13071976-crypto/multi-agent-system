from types import SimpleNamespace
import pytest

from product_enrichment.characteristics import extract_compact_feature_facts, merge_facts_into_characteristics
from product_enrichment.models import ProductIdentityQuery
from product_enrichment.identity import resolve_identity
from product_enrichment.research import research_product
from product_enrichment.orchestrator import enrich_product
from business_assistant.product_enrichment_bridge import build_enriched_write_request
from integrations.bitrix.schema import resolve_section_id


class Source:
    def __init__(self, pages):
        self.pages, self.fetched, self.queries = pages, [], []
    async def search(self, query, max_results=5):
        self.queries.append(query)
        return [SimpleNamespace(url=url, title=title, snippet="") for url, (title, body) in self.pages.items()]
    async def fetch_text(self, url):
        self.fetched.append(url)
        return self.pages[url][1]


@pytest.mark.parametrize("text", [
    "Прибл. 1444 x 832 x 56 мм", "VESA 300x300 мм", "Габариты 1444 x 832 мм",
    "Dimensions 1444 x 832 x 56 mm", "1444 x 832", "Screen resolution 1444 x 832 x 56",
])
def test_dimensions_never_become_resolution(text):
    assert "screen_resolution" not in dict(extract_compact_feature_facts(text))


def test_explicit_resolution_is_retained():
    assert dict(extract_compact_feature_facts("Разрешение экрана 3840 x 2160"))["screen_resolution"] == "3840 x 2160"


@pytest.mark.asyncio
async def test_ru_blog_and_wrong_support_page_are_not_fetched_or_used_for_images():
    source = Source({url: ("TCL 55C6K", "<h1>Телевизор TCL 55C6K</h1><p>Google TV</p>")
                     for url in ["https://dtf.ru/review/55c6k", "https://vc.ru/55c6k",
                                 "https://www.tcl.com/ru/ru/support-robot-vacuum-cleaner/model/55c6k",
                                 "https://www.tcl.com/ru/ru/tvs/55c6k"]})
    media = []
    facts = await research_product(resolve_identity(ProductIdentityQuery(
        brand="TCL", model="55C6K", market="RU")), search_port=source, fetch_port=source, media_sink=media)
    assert source.fetched == ["https://www.tcl.com/ru/ru/tvs/55c6k"]
    assert facts
    assert "-inurl:support" in source.queries[0]


@pytest.mark.asyncio
async def test_category_survives_zero_writable_specs_and_resolves_existing_section():
    source = Source({"https://www.technopark.ru/product/65rm7l":
        ("TCL 65RM7L", "<h1>Телевизор TCL 65RM7L</h1><p>Размер: 1444 x 832 x 56 мм</p>")})
    result = await enrich_product(tenant_id="t", query=ProductIdentityQuery(
        brand="TCL", model="65RM7L", market="RU"), search_port=source, fetch_port=source)
    request = build_enriched_write_request({"sku": "65RM7L", "brand": "TCL"},
        tenant_id="t", retail_price="88500", enrichment=result)
    assert request.characteristics == {}
    assert request.category_source == "tv"
    section = resolve_section_id(category=request.category_source,
        sections=[{"id": 70, "name": "Телевизоры"}])
    assert section["section_id"] == 70


@pytest.mark.asyncio
async def test_category_is_generic_for_non_tv_and_preserves_supplier_category():
    source = Source({"https://www.technopark.ru/product/rf100":
        ("ACME RF100", "<h1>Холодильник ACME RF100</h1>")})
    args = dict(tenant_id="t", search_port=source, fetch_port=source)
    result = await enrich_product(query=ProductIdentityQuery(brand="ACME", model="RF100", market="RU"), **args)
    assert result.identity.category == "refrigerator"
    explicit = await enrich_product(query=ProductIdentityQuery(
        brand="ACME", model="RF100", market="RU", category="Supplier category"), **args)
    assert explicit.identity.category == "Supplier category"


@pytest.mark.asyncio
async def test_conflicting_category_headings_remain_unresolved():
    source = Source({
        "https://www.technopark.ru/product/x100": ("ACME X100", "<h1>Телевизор ACME X100</h1>"),
        "https://www.mvideo.ru/product/x100": ("ACME X100", "<h1>Монитор ACME X100</h1>")})
    result = await enrich_product(tenant_id="t", query=ProductIdentityQuery(
        brand="ACME", model="X100", market="RU"), search_port=source, fetch_port=source)
    assert result.identity.category == ""


@pytest.mark.asyncio
async def test_contradictory_structured_specs_on_one_page_are_not_first_value_wins():
    source = Source({"https://www.tcl.com/ru/ru/tvs/65rm7l": ("TCL 65RM7L",
        "<p>Операционная система: Android TV</p><p>Операционная система: Google TV</p>")})
    facts = await research_product(resolve_identity(ProductIdentityQuery(
        brand="TCL", model="65RM7L", market="RU")), search_port=source, fetch_port=source)
    chars, conflicts = merge_facts_into_characteristics(facts)
    assert "operating_system" not in chars
    assert any(c.code == "characteristic_conflict_operating_system" for c in conflicts)


@pytest.mark.asyncio
async def test_size_selector_links_are_not_specs_of_selected_model():
    source = Source({"https://www.tcl.com/ru/ru/tvs/55c6k": ("Телевизор TCL 55C6K",
        '<h1>TCL TV C6K</h1><h4>Диагональ экрана</h4>'
        '<a href="/98c6k">98</a><a href="/65c6k">65</a><span>55</span>'
        '<h2>QD-Mini LED</h2><h2>HVA Panel</h2><h2>144Hz Native Refresh Rate</h2>')})
    result = await enrich_product(tenant_id="t", query=ProductIdentityQuery(
        brand="TCL", model="55C6K", market="RU"), search_port=source, fetch_port=source)
    assert result.identity.category == "tv"
    assert result.characteristics["panel_technology"].value == "HVA"
    assert result.characteristics["refresh_rate_hz"].value == "144"
    assert "screen_diagonal_cm" not in result.characteristics


@pytest.mark.asyncio
async def test_specific_backlight_does_not_get_lost_after_generic_headline():
    source = Source({"https://www.tcl.com/ru/ru/tvs/65rm7l": ("TCL 65RM7L",
        "<h1>Телевизор TCL 65RM7L</h1><h2>Mini LED</h2><h2>RGB Mini LED</h2>")})
    facts = await research_product(resolve_identity(ProductIdentityQuery(
        brand="TCL", model="65RM7L", market="RU")), search_port=source, fetch_port=source)
    chars, _ = merge_facts_into_characteristics(facts)
    assert chars["backlight_technology"].value == "RGB Mini LED"
