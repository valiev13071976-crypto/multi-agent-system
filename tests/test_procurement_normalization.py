"""Unit tests for offer normalization."""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from procurement.models import Money, OfferProvenance, SupplierOffer, content_hash_text
from procurement.normalizer import OfferNormalizer


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


class ProcurementNormalizationTests(unittest.TestCase):
    def test_unknown_fees_leave_total_none(self):
        normalizer = OfferNormalizer()
        offer = SupplierOffer(
            offer_id="o1",
            request_id="r1",
            supplier_id="s1",
            scope=_scope(),
            source_type="seed",
            source_ref="ref",
            currency="USD",
            unit_price=Money(amount=Decimal("10"), currency="USD"),
            quantity=Decimal("5"),
            provenance=_prov(),
            shipping_cost=None,
            tax=None,
        )
        normalized = normalizer.normalize(offer)
        self.assertIsNone(normalized.total_cost)
        self.assertEqual(normalized.subtotal.amount, Decimal("50"))

    def test_expired_offer_marked(self):
        stamp = utc_now()
        normalizer = OfferNormalizer()
        offer = SupplierOffer(
            offer_id="o1",
            request_id="r1",
            supplier_id="s1",
            scope=_scope(),
            source_type="seed",
            source_ref="ref",
            currency="USD",
            unit_price=Money(amount=Decimal("10"), currency="USD"),
            quantity=Decimal("5"),
            provenance=_prov(),
            valid_until=stamp - timedelta(days=1),
        )
        normalized = normalizer.normalize(offer, now=stamp)
        self.assertEqual(normalized.status, "expired")


if __name__ == "__main__":
    unittest.main()
