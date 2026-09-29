from types import SimpleNamespace

import pytest

from product_enrichment.identity import resolve_identity
from product_enrichment.models import ProductIdentityQuery
from product_enrichment.orchestrator import enrich_product
from product_enrichment.research import official_regional_links, research_product


class Discovery:
    def __init__(self):
        self.calls, self.fetched = [], []

    async def search(self, query, max_results=5):
        self.calls.append((query, max_results))
        if 'site:tcl.com' in query and '/ru' not in query:
            return [SimpleNamespace(url='https://www.tcl.com/global/en/tvs/65rm7l', title='TCL 65RM7L', snippet='')]
        return []

    async def fetch_text(self, url):
        self.fetched.append(url)
        if '/global/' in url:
            return ('<link href="https://www.tcl.com/ru/ru/tvs/65rm7l" hreflang="ru" rel="alternate">'
                    '<h1>Television TCL 65RM7L</h1><p>Operating system: FOREIGN_OS</p>'
                    '<meta property="og:image" content="https://www.tcl.com/foreign.jpg">')
        return ('<h1>Телевизор TCL 65RM7L</h1><h2>Google TV</h2><h2>RGB Mini LED</h2>'
                '<meta property="og:image" content="https://www.tcl.com/ru/product.jpg">')


@pytest.mark.asyncio
async def test_discovery_search_to_ru_page_to_content_and_category():
    port = Discovery()
    result = await enrich_product(tenant_id='t', query=ProductIdentityQuery(
        brand='TCL', model='65RM7L', market='RU'), search_port=port, fetch_port=port)
    assert result.identity.category == 'tv'
    assert result.characteristics['operating_system'].value == 'Google TV'
    assert result.characteristics['backlight_technology'].value == 'RGB Mini LED'
    assert 'FOREIGN_OS' not in str(result)
    assert len(port.calls) == 3
    assert sum(count for _, count in port.calls) <= 10
    assert 'site:mvideo.ru' in port.calls[2][0]
    assert port.fetched == ['https://www.tcl.com/global/en/tvs/65rm7l', 'https://www.tcl.com/ru/ru/tvs/65rm7l']


def test_declared_links_only_exact_model_official_ru_no_suffix_loss():
    identity = resolve_identity(ProductIdentityQuery(brand='LG', model='32LQ63006LA.ARUG', market='RU'))
    html = ''.join(f'<link href="{url}" rel="alternate" hreflang="ru">' for url in [
        'https://lg.com/ru/tv/32LQ63006LA',
        'https://lg.com/ru/tv/32LQ63006LA.ARUB',
        'https://lg.com.attacker.ru/ru/32LQ63006LA.ARUG',
        'https://lg.com/tr/tv/32LQ63006LA.ARUG',
        'https://lg.com/ru/support/32LQ63006LA.ARUG',
        'https://lg.com/ru/tv/32LQ63006LA.ARUG'])
    assert official_regional_links(html, base_url='https://lg.com/global/tv', identity=identity) == (
        'https://lg.com/ru/tv/32LQ63006LA.ARUG',)


@pytest.mark.asyncio
async def test_foreign_page_without_ru_link_does_not_supply_facts_or_media():
    class NoRussian(Discovery):
        async def fetch_text(self, url):
            return '<h1>TCL 65RM7L</h1><h2>Google TV</h2><meta property="og:image" content="https://tcl.com/x.jpg">'
    port = NoRussian()
    media = []
    facts = await research_product(resolve_identity(ProductIdentityQuery(
        brand='TCL', model='65RM7L', market='RU')), search_port=port, fetch_port=port, media_sink=media)
    assert facts == () and media == []


@pytest.mark.asyncio
async def test_discovery_fetch_bound_and_failure_are_safe():
    class Many(Discovery):
        async def search(self, query, max_results=5):
            return [SimpleNamespace(url=f'https://tcl.com/{region}/tvs/65rm7l', title='TCL 65RM7L', snippet='')
                    for region in ['global', 'eu', 'uk', 'tr', 'us']]
        async def fetch_text(self, url):
            self.fetched.append(url)
            raise RuntimeError('fetch failed')
    port = Many()
    facts = await research_product(resolve_identity(ProductIdentityQuery(
        brand='TCL', model='65RM7L', market='RU')), search_port=port, fetch_port=port)
    assert facts == ()
    assert len(port.fetched) == 2


@pytest.mark.asyncio
async def test_real_gateway_and_brave_adapter_follow_regional_link_for_each_product(monkeypatch):
    import httpx
    from tools.gateway import ToolGateway
    from tools.registry import ToolRegistry
    from tools.search.brave_provider import BraveSearchProvider
    from tools.platform.web_fetch_adapter import WebFetchAdapter
    from tools.platform.descriptors import scrape_fetch_descriptor
    from business_assistant.product_enrichment_bridge import run_enrichment, build_enriched_write_request
    from integrations.bitrix.schema import resolve_section_id

    monkeypatch.setenv('PANDA_CATALOG_RESEARCH_MARKET', 'RU')
    queries, pages = [], []

    def search(request):
        q = request.url.params['q']
        queries.append(q)
        model = next(m for m in ('55C6K', '65RM7L') if m in q)
        if 'site:tcl.com ' in q:
            rows = [{'title': f'TCL {model}', 'url': f'https://www.tcl.com/global/en/tvs/{model.lower()}', 'description': ''}]
        else:
            # Wrong-route indexed support results must not become evidence.
            rows = [{'title': f'TCL {model}', 'url': f'https://www.tcl.com/ru/ru/support/model/{model.lower()}', 'description': ''}]
        return httpx.Response(200, json={'web': {'results': rows}})

    def fetch(request):
        url = str(request.url)
        pages.append(url)
        model = url.rsplit('/', 1)[-1]
        if '/global/' in url:
            html = f'<link rel="alternate" hreflang="ru" href="https://www.tcl.com/ru/ru/tvs/{model}"><p>Operating system: FOREIGN_OS</p>'
        else:
            html = f'<h1>Телевизор TCL {model}</h1><h2>Google TV</h2><meta property="og:image" content="https://tcl.com/ru/{model}.jpg">'
        return httpx.Response(200, text=html)

    registry = ToolRegistry()
    registry.register(scrape_fetch_descriptor(enabled=True), adapter=WebFetchAdapter(transport=httpx.MockTransport(fetch)))
    gateway = ToolGateway(BraveSearchProvider(api_key='test-only', transport=httpx.MockTransport(search)),
                          registry=registry, register_search=False)
    for model, price in [('55C6K', '54500'), ('65RM7L', '88500')]:
        fields = {'brand': 'TCL', 'sku': model}
        enriched = await run_enrichment(tenant_id='tenant', product_fields=fields, tool_gateway=gateway, force_refresh=True)
        assert enriched.characteristics['operating_system'].value == 'Google TV'
        assert 'Google TV' in enriched.content.detailed_description
        write = build_enriched_write_request(fields, tenant_id='tenant', retail_price=price, enrichment=enriched)
        assert resolve_section_id(category=write.category_source, sections=[{'id': 70, 'name': 'Телевизоры'}])['section_id'] == 70
    assert len(queries) == 6
    assert len(pages) == 4
    assert not any('/support/' in url for url in pages)
