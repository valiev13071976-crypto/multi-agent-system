"""Unit tests for procurement risk analysis."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from procurement.models import Money, OfferProvenance, ProcurementRequirement, RiskFinding, SupplierOffer, content_hash_text
from procurement.risk import ProcurementRiskAnalyzer


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


class ProcurementRiskTests(unittest.TestCase):
    def test_single_source_risk(self):
        requirement = ProcurementRequirement(
            category="general",
            normalized_item="Widget",
            quantity=Decimal("10"),
            unit="pcs",
        )
        offer = SupplierOffer(
            offer_id="o1",
            request_id="r1",
            supplier_id="s1",
            scope=_scope(),
            source_type="seed",
            source_ref="ref",
            currency="USD",
            unit_price=Money(amount=Decimal("10"), currency="USD"),
            quantity=Decimal("10"),
            provenance=_prov(),
        )
        findings = ProcurementRiskAnalyzer().analyze(
            offers=(offer,),
            suppliers={},
            requirement=requirement,
            single_source=True,
        )
        codes = {f.code for f in findings}
        self.assertIn("single_source_procurement", codes)

    def test_unknown_fees_risk(self):
        requirement = ProcurementRequirement(
            category="general",
            normalized_item="Widget",
            quantity=Decimal("10"),
            unit="pcs",
        )
        offer = SupplierOffer(
            offer_id="o1",
            request_id="r1",
            supplier_id="s1",
            scope=_scope(),
            source_type="seed",
            source_ref="ref",
            currency="USD",
            unit_price=Money(amount=Decimal("10"), currency="USD"),
            quantity=Decimal("10"),
            provenance=_prov(),
            shipping_cost=None,
            tax=None,
        )
        findings = ProcurementRiskAnalyzer().analyze(
            offers=(offer,),
            suppliers={},
            requirement=requirement,
        )
        self.assertTrue(any(f.code == "unknown_fees" for f in findings))


if __name__ == "__main__":
    unittest.main()
