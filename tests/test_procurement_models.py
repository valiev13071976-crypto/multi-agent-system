"""Unit tests for procurement.models invariants."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from procurement.models import (
    Money,
    OfferProvenance,
    ProcurementRequest,
    Supplier,
    SupplierOffer,
    content_hash_text,
    parse_money_amount,
)


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


class ProcurementModelsTests(unittest.TestCase):
    def test_money_rejects_float(self):
        with self.assertRaises(ValueError):
            Money(amount=1.5, currency="USD")

    def test_money_requires_three_letter_currency(self):
        with self.assertRaises(ValueError):
            Money(amount=Decimal("1"), currency="US")

    def test_parse_money_amount_from_string(self):
        self.assertEqual(parse_money_amount("10.50"), Decimal("10.50"))

    def test_offer_provenance_requires_content_hash(self):
        with self.assertRaises(ValueError):
            OfferProvenance(
                source_id="s",
                source_ref="r",
                retrieved_at=utc_now(),
                content_hash="",
            )

    def test_request_quantity_none_allowed(self):
        req = ProcurementRequest(
            request_id="r1",
            scope=_scope(),
            requested_by="user",
            item_name="Widget",
            quantity=None,
            unit=None,
        )
        self.assertIsNone(req.quantity)

    def test_supplier_status_enum(self):
        with self.assertRaises(ValueError):
            Supplier(
                supplier_id="s1",
                scope=_scope(),
                name="Acme",
                source="seed",
                source_ref="r",
                status="bogus",
            )

    def test_offer_requires_provenance(self):
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
        )
        self.assertEqual(offer.currency, "USD")


if __name__ == "__main__":
    unittest.main()
