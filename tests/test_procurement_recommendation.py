"""Unit tests for procurement recommendation builder."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from procurement.models import (
    Money,
    OfferProvenance,
    ProcurementRequest,
    ProcurementRequirement,
    Supplier,
    SupplierOffer,
    content_hash_text,
)
from procurement.recommendation import ProcurementRecommendationService


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


class ProcurementRecommendationTests(unittest.TestCase):
    def _request(self):
        return ProcurementRequest(
            request_id="r1",
            scope=_scope(),
            requested_by="user",
            item_name="Widget",
            quantity=Decimal("10"),
            unit="pcs",
            specifications={"color": "blue"},
            currency="USD",
        )

    def test_restricted_supplier_not_recommended(self):
        requirement = ProcurementRequirement(
            category="general",
            normalized_item="Widget",
            quantity=Decimal("10"),
            unit="pcs",
            mandatory_specs={"color": "blue"},
        )
        scope = _scope()
        suppliers = {
            "s1": Supplier(
                supplier_id="s1",
                scope=scope,
                name="Blocked",
                source="seed",
                source_ref="r",
                status="restricted",
            ),
            "s2": Supplier(
                supplier_id="s2",
                scope=scope,
                name="Allowed",
                source="seed",
                source_ref="r2",
            ),
        }
        offers = (
            SupplierOffer(
                offer_id="o1",
                request_id="r1",
                supplier_id="s1",
                scope=scope,
                source_type="seed",
                source_ref="ref1",
                currency="USD",
                unit_price=Money(amount=Decimal("1"), currency="USD"),
                quantity=Decimal("10"),
                provenance=_prov("ref1"),
                specifications={"color": "blue"},
            ),
            SupplierOffer(
                offer_id="o2",
                request_id="r1",
                supplier_id="s2",
                scope=scope,
                source_type="seed",
                source_ref="ref2",
                currency="USD",
                unit_price=Money(amount=Decimal("20"), currency="USD"),
                quantity=Decimal("10"),
                provenance=_prov("ref2"),
                specifications={"color": "blue"},
            ),
        )
        rec = ProcurementRecommendationService().build(
            request=self._request(),
            requirement=requirement,
            offers=offers,
            suppliers=suppliers,
        )
        self.assertEqual(rec.recommended_supplier_id, "s2")

    def test_provenance_missing_metadata_excluded(self):
        requirement = ProcurementRequirement(
            category="general",
            normalized_item="Widget",
            quantity=Decimal("10"),
            unit="pcs",
            mandatory_specs={"color": "blue"},
        )
        scope = _scope()
        offers = (
            SupplierOffer(
                offer_id="o-bad",
                request_id="r1",
                supplier_id="s1",
                scope=scope,
                source_type="seed",
                source_ref="ref1",
                currency="USD",
                unit_price=Money(amount=Decimal("1"), currency="USD"),
                quantity=Decimal("10"),
                provenance=_prov("ref1"),
                specifications={"color": "blue"},
                metadata_safe={"price_provenance_missing": True},
            ),
            SupplierOffer(
                offer_id="o-good",
                request_id="r1",
                supplier_id="s2",
                scope=scope,
                source_type="seed",
                source_ref="ref2",
                currency="USD",
                unit_price=Money(amount=Decimal("20"), currency="USD"),
                quantity=Decimal("10"),
                provenance=_prov("ref2"),
                specifications={"color": "blue"},
            ),
        )
        rec = ProcurementRecommendationService().build(
            request=self._request(),
            requirement=requirement,
            offers=offers,
            suppliers={
                "s1": Supplier(
                    supplier_id="s1",
                    scope=scope,
                    name="A",
                    source="seed",
                    source_ref="r",
                ),
                "s2": Supplier(
                    supplier_id="s2",
                    scope=scope,
                    name="B",
                    source="seed",
                    source_ref="r2",
                ),
            },
        )
        self.assertEqual(rec.recommended_offer_id, "o-good")


if __name__ == "__main__":
    unittest.main()
