"""Unit tests for deterministic supplier comparison."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from procurement.comparator import SupplierComparator
from procurement.models import Money, OfferProvenance, ProcurementRequirement, Supplier, SupplierOffer, content_hash_text


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


class ProcurementComparatorTests(unittest.TestCase):
    def test_mandatory_spec_failure_caps_score(self):
        requirement = ProcurementRequirement(
            category="general",
            normalized_item="Widget",
            quantity=Decimal("10"),
            unit="pcs",
            mandatory_specs={"color": "blue"},
        )
        suppliers = {
            "s1": Supplier(
                supplier_id="s1",
                scope=_scope(),
                name="Bad",
                source="seed",
                source_ref="r",
            )
        }
        offer = SupplierOffer(
            offer_id="o1",
            request_id="r1",
            supplier_id="s1",
            scope=_scope(),
            source_type="seed",
            source_ref="ref",
            currency="USD",
            unit_price=Money(amount=Decimal("1"), currency="USD"),
            quantity=Decimal("10"),
            provenance=_prov(),
            specifications={"color": "red"},
        )
        rows = SupplierComparator().compare(
            offers=(offer,),
            suppliers=suppliers,
            requirement=requirement,
        )
        self.assertTrue(rows[0].mandatory_spec_failed)
        self.assertLessEqual(rows[0].score, Decimal("0"))

    def test_provenance_missing_flag_from_metadata(self):
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
            metadata_safe={"price_provenance_missing": True},
        )
        rows = SupplierComparator().compare(
            offers=(offer,),
            suppliers={},
            requirement=requirement,
        )
        self.assertIn("provenance_missing", rows[0].flags)


if __name__ == "__main__":
    unittest.main()
