"""Unit tests for procurement workflow stages."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from procurement.errors import PROCUREMENT_APPROVAL_REQUIRED, PROCUREMENT_ACTION_DENIED, ProcurementError
from procurement.models import (
    Money,
    OfferProvenance,
    ProcurementRequest,
    Supplier,
    SupplierOffer,
    content_hash_text,
)
from procurement.runtime import build_procurement_runtime


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


class ProcurementWorkflowTests(unittest.TestCase):
    def test_happy_path_waits_for_approval(self):
        scope = _scope()
        rt = build_procurement_runtime(env={"PROCUREMENT_ENABLED": "true"})
        svc = rt.service
        svc.create_request(
            ProcurementRequest(
                request_id="r1",
                scope=scope,
                requested_by="user",
                item_name="Widget",
                quantity=Decimal("10"),
                unit="pcs",
                specifications={"color": "blue"},
                currency="USD",
            ),
            requesting_scope=scope,
        )
        s1 = Supplier(
            supplier_id="s1",
            scope=scope,
            name="Acme",
            source="seed",
            source_ref="r",
        )
        s2 = Supplier(
            supplier_id="s2",
            scope=scope,
            name="Alt",
            source="seed",
            source_ref="r2",
        )
        offers = (
            SupplierOffer(
                offer_id="o1",
                request_id="r1",
                supplier_id="s1",
                scope=scope,
                source_type="seed",
                source_ref="ref1",
                currency="USD",
                unit_price=Money(amount=Decimal("10"), currency="USD"),
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
                unit_price=Money(amount=Decimal("12"), currency="USD"),
                quantity=Decimal("10"),
                provenance=_prov("ref2"),
                specifications={"color": "blue"},
            ),
        )
        result = rt.workflow.run(
            "r1",
            requesting_scope=scope,
            seed_suppliers=(s1, s2),
            seed_offers=offers,
            now=utc_now(),
        )
        self.assertEqual(result["status"], "waiting_approval")
        with self.assertRaises(ProcurementError) as ctx:
            svc.prepare_action("r1", requesting_scope=scope)
        self.assertEqual(ctx.exception.reason, PROCUREMENT_APPROVAL_REQUIRED)
        resolved = rt.workflow.resolve_approval(
            "r1", requesting_scope=scope, approved=True
        )
        self.assertIsNotNone(resolved.get("action"))
        with self.assertRaises(ProcurementError) as ctx2:
            svc.execute_financial_action("place_order")
        self.assertEqual(ctx2.exception.reason, PROCUREMENT_ACTION_DENIED)


if __name__ == "__main__":
    unittest.main()
