"""Unit tests for procurement observability hooks."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from observability.runtime import build_observability_runtime
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


class ProcurementObservabilityTests(unittest.TestCase):
    def test_request_created_emits_event(self):
        obs = build_observability_runtime(env={})
        rt = build_procurement_runtime(
            env={"PROCUREMENT_ENABLED": "true"},
            observability=obs,
        )
        scope = _scope()
        rt.service.create_request(
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
        types = [ev.event_type for ev in obs.list_events()]
        self.assertIn("procurement.request_created", types)

    def test_workflow_emits_recommendation_event(self):
        obs = build_observability_runtime(env={})
        rt = build_procurement_runtime(
            env={"PROCUREMENT_ENABLED": "true"},
            observability=obs,
        )
        scope = _scope()
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
        rt.workflow.run(
            "r1",
            requesting_scope=scope,
            seed_suppliers=(s1, s2),
            seed_offers=offers,
            now=utc_now(),
        )
        types = [ev.event_type for ev in obs.list_events()]
        self.assertIn("procurement.recommendation_created", types)


if __name__ == "__main__":
    unittest.main()
