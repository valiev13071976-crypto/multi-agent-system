"""Unit tests for procurement decision memory persistence."""

from __future__ import annotations

import unittest
from decimal import Decimal

from memory.models import MEMORY_SEMANTIC, MemoryQuery, SCOPE_PROJECT, MemoryScope, utc_now
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from procurement.models import (
    Money,
    OfferProvenance,
    ProcurementRecommendation,
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


class ProcurementMemoryTests(unittest.TestCase):
    def test_persist_decision_memory(self):
        scope = _scope()
        mem = MemoryService(InMemoryMemoryStore())
        rt = build_procurement_runtime(
            env={"PROCUREMENT_ENABLED": "true"},
            memory_service=mem,
        )
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
        rec = ProcurementRecommendation(
            recommendation_id="rec1",
            request_id="r1",
            scope=scope,
            recommended_supplier_id="s1",
            recommended_offer_id="o1",
            alternatives=(),
            reasoning_summary="Selected best offer",
            comparison=(),
            risks=(),
            assumptions=(),
            missing_information=(),
            confidence=0.8,
            citations=("knowledge:manual:1",),
            requires_approval=True,
            status="recommendation_ready",
        )
        svc.request_store.save_recommendation(rec)
        svc.persist_decision_memory("r1", requesting_scope=scope)
        hits = mem.retrieve(MemoryQuery(query_text="Procurement decision", scope=scope))
        self.assertTrue(hits)
        self.assertIn("procurement", hits[0].tags)


if __name__ == "__main__":
    unittest.main()
