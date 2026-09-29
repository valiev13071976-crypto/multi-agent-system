from dataclasses import replace
from types import SimpleNamespace

import pytest

from product_enrichment.market import catalog_market, russian_source
from product_enrichment.identity import resolve_identity
from product_enrichment.models import ProductIdentityQuery
from product_enrichment.research import research_product
from product_enrichment.cache import EnrichmentCache
from business_assistant.product_enrichment_bridge import run_enrichment


@pytest.mark.parametrize("url,accepted", [
    ("https://www.tcl.com/ru/ru/tvs/55c6k", True),
    ("https://www.lg.com/ru/tvs/model", True),
    ("https://shop.ru/product", True),
    ("https://www.tcl.com/tr/tr/tvs/model", False),
    ("https://www.tcl.com/content/dam/brandsite/region/australia/model.pdf", False),
    ("https://shop.ru.attacker.com/product", False),
    ("https://www.tcl.com/global/en/tvs/model?ru=true", False),
])
def test_market_filter(url, accepted):
    assert russian_source(url) is accepted


def test_tenant_override_and_market_cache_isolation(monkeypatch):
    monkeypatch.setenv("PANDA_CATALOG_RESEARCH_MARKET", "RU")
    monkeypatch.setenv("PANDA_CATALOG_RESEARCH_MARKETS", '{"other": ""}')
    assert catalog_market("panda") == "RU"
    assert catalog_market("other") == ""
    query = ProductIdentityQuery(brand="LG", model="32LQ63006LA.ARUG")
    assert resolve_identity(query).identity_key != resolve_identity(replace(query, market="RU")).identity_key


class Sources:
    def __init__(self, fail_secondary=False):
        self.queries, self.fetched = [], []
        self.fail_secondary = fail_secondary
    async def search(self, query, max_results=5):
        self.queries.append(query)
        if self.fail_secondary and len(self.queries) == 2:
            raise RuntimeError("budget exhausted")
        return [SimpleNamespace(url=url, title="TCL 55C6K", snippet="") for url in (
            "https://www.tcl.com/ru/ru/tvs/55c6k",
            "https://www.tcl.com/tr/tr/tvs/55c6k",
            "https://shop.ru/55c6k")]
    async def fetch_text(self, url):
        self.fetched.append(url)
        return "<p>55C6K</p><p>Операционная система: Google TV</p>"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_ru_official_search_first_foreign_results_never_fetched(failure):
    source = Sources(failure)
    facts = await research_product(resolve_identity(ProductIdentityQuery(
        brand="TCL", model="55C6K", market="RU")), search_port=source, fetch_port=source)
    assert "site:tcl.com/ru/" in source.queries[0]
    assert len(source.queries) == 2
    assert not any("/tr/" in url for url in source.fetched)
    assert len(source.fetched) == len(set(source.fetched))
    assert facts and all(russian_source(f.source_url) for f in facts)


@pytest.mark.asyncio
async def test_fresh_request_replaces_cache_instead_of_reusing_it(monkeypatch):
    from test_product_enrichment_bridge import _FakeToolGateway, _search_result
    monkeypatch.setenv("PANDA_CATALOG_RESEARCH_MARKET", "RU")
    url = "https://www.lg.com/ru/tvs/32LQ63006LA.ARUG"
    gateway = _FakeToolGateway(search_results=[_search_result(url, "LG 32LQ63006LA.ARUG")],
        page_text_by_url={url: "<p>Операционная система: webOS</p>"})
    cache = EnrichmentCache()
    kwargs = dict(tenant_id="t", product_fields={"brand": "LG", "sku": "32LQ63006LA.ARUG"},
                  tool_gateway=gateway, cache=cache)
    first = await run_enrichment(**kwargs)
    assert first.characteristics["operating_system"].value == "webOS"
    gateway._page_text_by_url[url] = "<p>Операционная система: Google TV</p>"
    old = await run_enrichment(**kwargs)
    assert old.cache_hit and old.characteristics["operating_system"].value == "webOS"
    fresh = await run_enrichment(**kwargs, force_refresh=True)
    assert not fresh.cache_hit and fresh.characteristics["operating_system"].value == "Google TV"
    again = await run_enrichment(**kwargs)
    assert again.cache_hit and again.characteristics["operating_system"].value == "Google TV"
