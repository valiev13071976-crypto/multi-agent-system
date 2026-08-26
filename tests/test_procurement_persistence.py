"""Durable SQLite procurement persistence and restart tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from memory.models import SCOPE_PROJECT, MemoryScope, utc_now
from procurement.models import (
    Money,
    OfferProvenance,
    ProcurementRequest,
    Supplier,
    SupplierOffer,
    content_hash_text,
)
from procurement.runtime import build_procurement_runtime
from procurement.sqlite_store import SqliteProcurementStore
from security.encryption import SENSITIVITY_SENSITIVE, EncryptionService


def _scope(sid="restart"):
    return MemoryScope(scope_type=SCOPE_PROJECT, scope_id=sid)


def _prov(ref="ref-1"):
    return OfferProvenance(
        source_id="doc.1",
        source_ref=ref,
        retrieved_at=utc_now(),
        content_hash=content_hash_text(ref),
        trust="document_sourced",
        freshness="static",
        document_id="d1",
        chunk_id="c1",
    )


class ProcurementSqlitePersistenceTests(unittest.TestCase):
    def test_restart_survives_request_supplier_offer_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proc.sqlite3"
            env = {
                "PROCUREMENT_ENABLED": "true",
                "PROCUREMENT_BACKEND": "sqlite",
                "PROCUREMENT_DB_PATH": str(path),
            }
            scope = _scope()
            rt1 = build_procurement_runtime(env=env)
            self.assertIsNotNone(rt1)
            self.assertEqual(rt1.health()["persistence_backend"], "sqlite")
            self.assertTrue(rt1.health()["persistence_ready"])

            req = ProcurementRequest(
                request_id="r-restart",
                scope=scope,
                requested_by="buyer",
                item_name="Industrial Widget",
                quantity=Decimal("12"),
                unit="pcs",
                specifications={"color": "blue"},
                currency="USD",
                target_budget=Money(amount=Decimal("5000.00"), currency="USD"),
            )
            rt1.service.create_request(req, requesting_scope=scope)
            supplier = Supplier(
                supplier_id="sup-1",
                scope=scope,
                name="Acme Supply",
                source="manual",
                source_ref="manual:acme",
                categories=("general",),
                trust_level="known_internal",
                status="known",
            )
            rt1.service.supplier_repo.upsert(supplier)
            offer = SupplierOffer(
                offer_id="off-1",
                request_id="r-restart",
                supplier_id="sup-1",
                scope=scope,
                source_type="document",
                source_ref="quote:1",
                currency="USD",
                unit_price=Money(amount=Decimal("1234567890.123456789"), currency="USD"),
                quantity=Decimal("12"),
                provenance=_prov("quote:1"),
                shipping_cost=Money(amount=Decimal("10.00"), currency="USD"),
                tax=Money(amount=Decimal("5.00"), currency="USD"),
                specifications={"color": "blue"},
                status="validated",
                confidence=0.9,
                metadata_safe={"citation_ref": "document:d1#chunk:c1"},
            )
            rt1.service.offer_repo.upsert(offer)
            rt1.service.normalize_requirements("r-restart", requesting_scope=scope)
            rec = rt1.service.build_recommendation(
                "r-restart",
                requesting_scope=scope,
                citations=("document:d1#chunk:c1", "knowledge:k1"),
            )
            self.assertEqual(rec.citations, ("document:d1#chunk:c1", "knowledge:k1"))
            self.assertEqual(offer.unit_price.amount, Decimal("1234567890.123456789"))
            rt1.close()

            rt2 = build_procurement_runtime(env=env)
            got_req = rt2.service.get_request("r-restart", requesting_scope=scope)
            self.assertIsNotNone(got_req)
            self.assertEqual(got_req.item_name, "Industrial Widget")
            self.assertEqual(got_req.quantity, Decimal("12"))
            self.assertEqual(got_req.target_budget.amount, Decimal("5000.00"))

            got_sup = rt2.service.supplier_repo.get("sup-1", requesting_scope=scope)
            self.assertIsNotNone(got_sup)
            self.assertEqual(got_sup.name, "Acme Supply")

            got_offer = rt2.service.offer_repo.get("off-1", requesting_scope=scope)
            self.assertIsNotNone(got_offer)
            self.assertEqual(got_offer.unit_price.amount, Decimal("1234567890.123456789"))
            self.assertEqual(got_offer.provenance.content_hash, offer.provenance.content_hash)
            self.assertEqual(got_offer.provenance.document_id, "d1")
            self.assertEqual(got_offer.provenance.chunk_id, "c1")

            got_rec = rt2.service.request_store.get_recommendation_for_request(
                "r-restart", requesting_scope=scope
            )
            self.assertIsNotNone(got_rec)
            self.assertEqual(got_rec.citations, ("document:d1#chunk:c1", "knowledge:k1"))
            self.assertEqual(got_rec.recommended_offer_id, rec.recommended_offer_id)
            other = _scope("other")
            with self.assertRaises(Exception):
                rt2.service.get_request("r-restart", requesting_scope=other)
            rt2.close()

    def test_decimal_roundtrip_no_float_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "money.sqlite3"
            store = SqliteProcurementStore(db_path=path)
            scope = _scope("money")
            amount = Decimal("1234567890.123456789")
            offer = SupplierOffer(
                offer_id="off-money",
                request_id="r-money",
                supplier_id="s1",
                scope=scope,
                source_type="manual",
                source_ref="m:1",
                currency="USD",
                unit_price=Money(amount=amount, currency="USD"),
                quantity=Decimal("1"),
                provenance=_prov("m:1"),
                status="normalized",
            )
            store._upsert_offer(offer)
            store.close()
            store2 = SqliteProcurementStore(db_path=path)
            got = store2._get_offer("off-money", requesting_scope=scope)
            self.assertEqual(got.unit_price.amount, amount)
            self.assertNotIsInstance(got.unit_price.amount, float)
            store2.close()

    def test_provenance_and_citations_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prov.sqlite3"
            env = {
                "PROCUREMENT_ENABLED": "true",
                "PROCUREMENT_BACKEND": "sqlite",
                "PROCUREMENT_DB_PATH": str(path),
            }
            scope = _scope("prov")
            rt1 = build_procurement_runtime(env=env)
            rt1.service.create_request(
                ProcurementRequest(
                    request_id="r-prov",
                    scope=scope,
                    requested_by="u",
                    item_name="Bolt",
                    quantity=Decimal("1"),
                    unit="pcs",
                    currency="USD",
                ),
                requesting_scope=scope,
            )
            rt1.service.supplier_repo.upsert(
                Supplier(
                    supplier_id="s-prov",
                    scope=scope,
                    name="BoltCo",
                    source="document",
                    source_ref="doc:1",
                    trust_level="document_sourced",
                    status="known",
                )
            )
            prov = _prov("xlsx:sheet1")
            rt1.service.offer_repo.upsert(
                SupplierOffer(
                    offer_id="o-prov",
                    request_id="r-prov",
                    supplier_id="s-prov",
                    scope=scope,
                    source_type="document",
                    source_ref="xlsx:sheet1",
                    currency="USD",
                    unit_price=Money(amount=Decimal("9.99"), currency="USD"),
                    quantity=Decimal("1"),
                    provenance=prov,
                    shipping_cost=Money(amount=Decimal("0"), currency="USD"),
                    tax=Money(amount=Decimal("0"), currency="USD"),
                    status="validated",
                )
            )
            rt1.service.normalize_requirements("r-prov", requesting_scope=scope)
            cites = ("document:abc#chunk:1", "memory:m1")
            rec = rt1.service.build_recommendation(
                "r-prov", requesting_scope=scope, citations=cites
            )
            rt1.close()
            rt2 = build_procurement_runtime(env=env)
            offer = rt2.service.offer_repo.get("o-prov", requesting_scope=scope)
            self.assertEqual(offer.provenance.source_ref, "xlsx:sheet1")
            self.assertEqual(offer.provenance.content_hash, prov.content_hash)
            rec2 = rt2.service.request_store.get_recommendation_for_request(
                "r-prov", requesting_scope=scope
            )
            self.assertEqual(rec2.citations, cites)
            self.assertEqual(rec2.recommendation_id, rec.recommendation_id)
            rt2.close()

    def test_sensitive_offer_requires_encryption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sens.sqlite3"
            store = SqliteProcurementStore(db_path=path, encryption=None)
            scope = _scope("sens")
            offer = SupplierOffer(
                offer_id="o-sens",
                request_id="r-sens",
                supplier_id="s1",
                scope=scope,
                source_type="manual",
                source_ref="s",
                currency="USD",
                unit_price=Money(amount=Decimal("1.00"), currency="USD"),
                quantity=Decimal("1"),
                provenance=_prov("s"),
                metadata_safe={"sensitivity": SENSITIVITY_SENSITIVE},
            )
            with self.assertRaises(Exception):
                store._upsert_offer(offer)
            store.close()

            key = AESGCM.generate_key(bit_length=256)
            enc = EncryptionService(key=key, key_id="v1")
            store2 = SqliteProcurementStore(db_path=path, encryption=enc)
            store2._upsert_offer(offer)
            store2.close()
            store3 = SqliteProcurementStore(db_path=path, encryption=enc)
            got = store3._get_offer("o-sens", requesting_scope=scope)
            self.assertEqual(got.unit_price.amount, Decimal("1.00"))
            # ciphertext at rest
            raw = path.read_bytes()
            self.assertNotIn(b"1.00", raw)
            store3.close()

    def test_configured_sqlite_without_path_or_shared_is_blocked(self):
        rt = build_procurement_runtime(
            env={
                "PROCUREMENT_ENABLED": "true",
                "PROCUREMENT_BACKEND": "sqlite",
            },
            shared_connection=None,
        )
        self.assertIsNotNone(rt)
        self.assertEqual(rt.health()["procurement_status"], "blocked")
        self.assertFalse(rt.health()["persistence_ready"])
        self.assertIsNotNone(rt.service.blocked_reason)

    def test_memory_backend_still_isolated_across_runtimes(self):
        scope = _scope("mem")
        rt1 = build_procurement_runtime(env={"PROCUREMENT_ENABLED": "true", "PROCUREMENT_BACKEND": "memory"})
        rt1.service.create_request(
            ProcurementRequest(
                request_id="r-mem",
                scope=scope,
                requested_by="u",
                item_name="X",
                quantity=Decimal("1"),
                unit="pcs",
            ),
            requesting_scope=scope,
        )
        rt2 = build_procurement_runtime(env={"PROCUREMENT_ENABLED": "true", "PROCUREMENT_BACKEND": "memory"})
        self.assertIsNone(rt2.service.get_request("r-mem", requesting_scope=scope))

    def test_no_duplicate_offer_primary_on_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dup.sqlite3"
            store = SqliteProcurementStore(db_path=path)
            scope = _scope("dup")
            offer = SupplierOffer(
                offer_id="o-dup",
                request_id="r-dup",
                supplier_id="s1",
                scope=scope,
                source_type="manual",
                source_ref="once",
                currency="USD",
                unit_price=Money(amount=Decimal("2.00"), currency="USD"),
                quantity=Decimal("1"),
                provenance=_prov("once"),
                status="discovered",
            )
            store._upsert_offer(offer)
            updated = SupplierOffer(
                offer_id="o-dup",
                request_id="r-dup",
                supplier_id="s1",
                scope=scope,
                source_type="manual",
                source_ref="once",
                currency="USD",
                unit_price=Money(amount=Decimal("3.00"), currency="USD"),
                quantity=Decimal("1"),
                provenance=_prov("once"),
                status="normalized",
            )
            store._upsert_offer(updated)
            rows = store.list_for_request("r-dup", scope=scope)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].unit_price.amount, Decimal("3.00"))
            store.close()


if __name__ == "__main__":
    unittest.main()
