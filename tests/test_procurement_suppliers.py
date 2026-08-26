"""Unit tests for supplier discovery and repository."""

from __future__ import annotations

import unittest

from memory.models import SCOPE_PROJECT, MemoryScope
from procurement.discovery import SupplierDiscoveryService
from procurement.models import ProcurementRequirement, Supplier
from procurement.repos import InMemorySupplierRepository


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _requirement(**kwargs):
    defaults = dict(
        category="general",
        normalized_item="Widget",
        quantity=None,
        unit="pcs",
        mandatory_specs={"color": "blue"},
    )
    defaults.update(kwargs)
    return ProcurementRequirement(**defaults)


class ProcurementSuppliersTests(unittest.TestCase):
    def test_discovery_seeds_suppliers(self):
        repo = InMemorySupplierRepository()
        discovery = SupplierDiscoveryService(supplier_repo=repo)
        scope = _scope()
        seed = Supplier(
            supplier_id="s1",
            scope=scope,
            name="Acme",
            source="seed",
            source_ref="r1",
            status="known",
        )
        found = discovery.discover(
            scope=scope,
            requirement=_requirement(),
            seed_suppliers=(seed,),
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].supplier_id, "s1")

    def test_repo_excludes_restricted_when_requested(self):
        repo = InMemorySupplierRepository()
        scope = _scope()
        repo.upsert(
            Supplier(
                supplier_id="s1",
                scope=scope,
                name="Blocked",
                source="seed",
                source_ref="r",
                status="restricted",
            )
        )
        repo.upsert(
            Supplier(
                supplier_id="s2",
                scope=scope,
                name="Allowed",
                source="seed",
                source_ref="r2",
                status="known",
            )
        )
        rows = repo.find(scope=scope, exclude_restricted=True)
        self.assertEqual([s.supplier_id for s in rows], ["s2"])


if __name__ == "__main__":
    unittest.main()
