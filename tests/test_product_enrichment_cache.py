"""Product enrichment pipeline: in-process enrichment cache (requirement
13) -- keyed by (tenant_id, identity_key), never a new infrastructure
stack."""

from __future__ import annotations

import unittest

from product_enrichment.cache import EnrichmentCache
from product_enrichment.identity import resolve_identity
from product_enrichment.models import EnrichmentResult, ProductIdentityQuery


def _result():
    identity = resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG"))
    return EnrichmentResult(identity=identity)


class EnrichmentCacheTests(unittest.TestCase):
    def test_get_on_empty_cache_returns_none(self):
        cache = EnrichmentCache()
        self.assertIsNone(cache.get(tenant_id="t1", identity_key="abc"))

    def test_put_then_get_round_trips(self):
        cache = EnrichmentCache()
        result = _result()
        cache.put(tenant_id="t1", identity_key=result.identity.identity_key, result=result)
        fetched = cache.get(tenant_id="t1", identity_key=result.identity.identity_key)
        self.assertIs(fetched, result)

    def test_entries_are_scoped_per_tenant(self):
        cache = EnrichmentCache()
        result = _result()
        cache.put(tenant_id="tenant-a", identity_key=result.identity.identity_key, result=result)
        self.assertIsNone(cache.get(tenant_id="tenant-b", identity_key=result.identity.identity_key))

    def test_clear_removes_only_the_targeted_entry(self):
        cache = EnrichmentCache()
        result = _result()
        cache.put(tenant_id="t1", identity_key="key-a", result=result)
        cache.put(tenant_id="t1", identity_key="key-b", result=result)
        cache.clear(tenant_id="t1", identity_key="key-a")
        self.assertIsNone(cache.get(tenant_id="t1", identity_key="key-a"))
        self.assertIsNotNone(cache.get(tenant_id="t1", identity_key="key-b"))

    def test_len_reflects_entry_count(self):
        cache = EnrichmentCache()
        result = _result()
        self.assertEqual(len(cache), 0)
        cache.put(tenant_id="t1", identity_key="key-a", result=result)
        self.assertEqual(len(cache), 1)


if __name__ == "__main__":
    unittest.main()
