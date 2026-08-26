"""Unit tests for ProcurementValidator."""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from procurement.errors import PROCUREMENT_PROVENANCE_MISSING
from procurement.models import Money, OfferProvenance, SupplierOffer, content_hash_text
from procurement.validator import ProcurementValidator


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


class ProcurementValidatorTests(unittest.TestCase):
    def test_provenance_missing_metadata_flagged(self):
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
            metadata_safe={"price_provenance_missing": True},
        )
        issues = ProcurementValidator().validate_offer(offer, require_provenance=True)
        self.assertIn(PROCUREMENT_PROVENANCE_MISSING, issues)

    def test_currency_mismatch_flag(self):
        offers = (
            SupplierOffer(
                offer_id="o1",
                request_id="r1",
                supplier_id="s1",
                scope=_scope(),
                source_type="seed",
                source_ref="ref",
                currency="USD",
                unit_price=Money(amount=Decimal("10"), currency="USD"),
                quantity=Decimal("5"),
                provenance=_prov("a"),
            ),
            SupplierOffer(
                offer_id="o2",
                request_id="r1",
                supplier_id="s2",
                scope=_scope(),
                source_type="seed",
                source_ref="ref2",
                currency="EUR",
                unit_price=Money(amount=Decimal("9"), currency="EUR"),
                quantity=Decimal("5"),
                provenance=_prov("b"),
            ),
        )
        self.assertTrue(ProcurementValidator().assert_no_fx_needed_or_flag(offers))

    def test_expired_offer_issue(self):
        stamp = utc_now()
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
        issues = ProcurementValidator().validate_offer(offer, now=stamp)
        self.assertIn("procurement_offer_expired", issues)


if __name__ == "__main__":
    unittest.main()
