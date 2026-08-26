"""Unit tests for procurement knowledge integration."""

from __future__ import annotations

import unittest
from decimal import Decimal

from knowledge.models import TRUST_OPERATOR, KnowledgeIngestRequest
from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from memory.service import MemoryService
from memory.store import InMemoryMemoryStore
from procurement.models import (
    Money,
    OfferProvenance,
    ProcurementRequest,
    Supplier,
    SupplierOffer,
    content_hash_text,
)
from procurement.runtime import build_procurement_runtime
from security.encryption import SENSITIVITY_INTERNAL


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


class ProcurementKnowledgeTests(unittest.TestCase):
    def test_knowledge_citations_in_recommendation(self):
        scope = _scope()
        mem = MemoryService(InMemoryMemoryStore())
        from knowledge.models import SOURCE_MANUAL_REFERENCE, TRUST_OPERATOR, FreshnessPolicy, KnowledgeSource
        from knowledge.registry import KnowledgeSourceRegistry
        from knowledge.service import KnowledgeService
        from memory.models import utc_now

        registry = KnowledgeSourceRegistry()
        ksvc = KnowledgeService(registry, memory_service=mem)
        stamp = utc_now()
        ksvc.register_source(
            KnowledgeSource(
                source_id="manual.default",
                scope=scope,
                source_type=SOURCE_MANUAL_REFERENCE,
                name="Manual",
                trust_level=TRUST_OPERATOR,
                refresh_policy=FreshnessPolicy(policy="static"),
                created_at=stamp,
                updated_at=stamp,
            )
        )
        item = ksvc.ingest(
            KnowledgeIngestRequest(
                scope=scope,
                source_id="manual.default",
                content="Widget supplier retention policy ninety days",
                trust_level=TRUST_OPERATOR,
                provenance_source_ref="manual:cite",
                sensitivity=SENSITIVITY_INTERNAL,
                validated=True,
            ),
            requesting_scope=scope,
        )
        rt = build_procurement_runtime(
            env={"PROCUREMENT_ENABLED": "true"},
            knowledge_service=ksvc,
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
        rec = result.get("recommendation")
        self.assertIsNotNone(rec)
        self.assertIn(item.citation_ref, rec.citations)


if __name__ == "__main__":
    unittest.main()
