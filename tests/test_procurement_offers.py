"""Unit tests for offer repository and validation status."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from procurement.models import Money, OfferProvenance, SupplierOffer, content_hash_text
from procurement.repos import InMemoryOfferRepository


def _scope(sid="p1"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _prov(ref="ref"):
    return OfferProvenance(
        source_id="src",
        source_ref=ref,
        retrieved_at=utc_now(),
        content_hash=content_hash_text(ref),
        trust="document_sourced",
    )


def _offer(offer_id="o1", request_id="r1"):
    return SupplierOffer(
        offer_id=offer_id,
        request_id=request_id,
        supplier_id="s1",
        scope=_scope(),
        source_type="seed",
        source_ref="ref",
        currency="USD",
        unit_price=Money(amount=Decimal("10"), currency="USD"),
        quantity=Decimal("5"),
        provenance=_prov(),
    )


class ProcurementOffersTests(unittest.TestCase):
    def test_offer_repo_lists_by_request(self):
        repo = InMemoryOfferRepository()
        scope = _scope()
        repo.upsert(_offer("o1"))
        repo.upsert(_offer("o2"))
        rows = repo.list_for_request("r1", scope=scope)
        self.assertEqual(len(rows), 2)

    def test_cross_scope_get_denied(self):
        from procurement.errors import PROCUREMENT_SCOPE_DENIED, ProcurementError

        repo = InMemoryOfferRepository()
        repo.upsert(_offer())
        with self.assertRaises(ProcurementError) as ctx:
            repo.get("o1", requesting_scope=_scope("other"))
        self.assertEqual(ctx.exception.reason, PROCUREMENT_SCOPE_DENIED)


if __name__ == "__main__":
    unittest.main()
