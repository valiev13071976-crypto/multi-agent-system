"""Unit tests for external knowledge via ToolGateway."""

from __future__ import annotations

import unittest

from knowledge.adapters import SearchProviderKnowledgeAdapter
from knowledge.models import (
    SOURCE_SEARCH_PROVIDER,
    TRUST_READ_ONLY_EXTERNAL,
    FreshnessPolicy,
    KnowledgeQuery,
    KnowledgeSource,
)
from knowledge.registry import KnowledgeSourceRegistry
from knowledge.service import KnowledgeService
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from tools.gateway import ToolGateway
from tools.search.fake_provider import FakeSearchProvider, fake_result


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


class CountingGateway(ToolGateway):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.search_calls = 0

    async def search(self, query, max_results=5, **kwargs):
        self.search_calls += 1
        return await super().search(query, max_results=max_results, **kwargs)


class KnowledgeToolGatewayTests(unittest.TestCase):
    def test_search_via_gateway_no_write_path(self):
        gw = CountingGateway(
            FakeSearchProvider(
                {"widget": [fake_result("https://en.wikipedia.org/wiki/Widget", snippet="Widget info")]}
            )
        )
        registry = KnowledgeSourceRegistry()
        svc = KnowledgeService(registry, tool_gateway=gw)
        scope = _scope("gw")
        stamp = utc_now()
        adapter = SearchProviderKnowledgeAdapter(gw, source_id="search.1")
        svc.register_source(
            KnowledgeSource(
                source_id="search.1",
                scope=scope,
                source_type=SOURCE_SEARCH_PROVIDER,
                name="Search",
                trust_level=TRUST_READ_ONLY_EXTERNAL,
                refresh_policy=FreshnessPolicy(policy="on_demand"),
                created_at=stamp,
                updated_at=stamp,
            ),
            adapter=adapter,
        )
        before_items = len(svc._items)
        rows = svc.retrieve(
            KnowledgeQuery(
                query_text="widget",
                scope=scope,
                allow_ephemeral_external=True,
                source_ids=("search.1",),
            )
        )
        self.assertGreaterEqual(gw.search_calls, 1)
        self.assertTrue(rows)
        self.assertEqual(len(svc._items), before_items)
        self.assertTrue(rows[0].citation_ref.startswith("external:"))


if __name__ == "__main__":
    unittest.main()
