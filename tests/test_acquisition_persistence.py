"""Durable acquisition SQLite store + production composition wiring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from acquisition.models import (
    CHANGE_CREATED,
    CONTENT_TRUST_UNTRUSTED,
    FreshnessPolicy,
    SourceDescriptor,
    TRUST_CONTRACTED_SUPPLIER,
)
from acquisition.runtime import build_acquisition_runtime
from acquisition.sqlite_store import SqliteAcquisitionStore
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets
from tools.gateway import ToolGateway


VALID_EAN = "5901234123457"


def _source(tenant="tenant-a", source_id="src-1"):
    return SourceDescriptor(
        source_id=source_id,
        source_type="supplier",
        tenant_id=tenant,
        trust_level=TRUST_CONTRACTED_SUPPLIER,
        freshness_policy=FreshnessPolicy(stale_after_seconds=3600),
        tool_id="http.request",
        enabled=True,
        allowed_domains=("example.com",),
        name="Supplier",
    )


class SqliteAcquisitionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "acq.db")
        self._stores: list = []

    def _store(self):
        store = SqliteAcquisitionStore(db_path=self.db_path)
        self._stores.append(store)
        return store

    def tearDown(self):
        for store in self._stores:
            try:
                store.close()
            except Exception:
                pass
        self._stores.clear()
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def test_restart_preserves_artifact_record_change_provenance(self):
        store1 = self._store()
        svc1 = build_acquisition_runtime(store=store1)
        svc1.register_source(_source())
        csv_text = (
            f"sku,ean,name,price,currency,stock\nS1,{VALID_EAN},Widget,10,USD,5\n"
        )
        art = svc1.ingest_text(
            source_id="src-1",
            tenant_id="tenant-a",
            text=csv_text,
            content_type="text/csv",
        )
        records = svc1.parse(art)
        self.assertEqual(len(records), 1)
        rec_id = records[0].record_id
        art_id = art.artifact_id
        checksum = art.checksum
        fingerprint = records[0].fingerprint
        changes = svc1.list_changes(tenant_id="tenant-a", source_id="src-1")
        self.assertTrue(any(c.outcome == CHANGE_CREATED for c in changes))
        prov = svc1.get_provenance(rec_id, tenant_id="tenant-a")
        self.assertEqual(prov["artifact"]["checksum"], checksum)
        store1.close()

        store2 = self._store()
        svc2 = build_acquisition_runtime(store=store2)
        # Source hydrated
        self.assertEqual(svc2.get_source("src-1", tenant_id="tenant-a").source_id, "src-1")
        self.assertIsNotNone(svc2.store.get_artifact(art_id, tenant_id="tenant-a"))
        self.assertIsNotNone(svc2.get_record(rec_id, tenant_id="tenant-a"))
        self.assertIsNotNone(
            svc2.store.find_artifact_by_checksum(checksum, tenant_id="tenant-a")
        )
        self.assertIsNotNone(
            svc2.store.find_record_by_fingerprint(fingerprint, tenant_id="tenant-a")
        )
        self.assertGreaterEqual(len(svc2.list_changes(tenant_id="tenant-a")), 1)
        prov2 = svc2.get_provenance(rec_id, tenant_id="tenant-a")
        self.assertEqual(prov2["parser"]["parser_id"], "price.csv")
        self.assertEqual(prov2["artifact"]["content_trust"], CONTENT_TRUST_UNTRUSTED)

    def test_tenant_isolation_at_query_level(self):
        store = self._store()
        svc = build_acquisition_runtime(store=store)
        svc.register_source(_source("tenant-a", "src-a"))
        svc.register_source(_source("tenant-b", "src-b"))
        art_a = svc.ingest_text(
            source_id="src-a",
            tenant_id="tenant-a",
            text="sku,price\nA,1\n",
            content_type="text/csv",
        )
        svc.parse(art_a)
        self.assertEqual(len(svc.list_records(tenant_id="tenant-a")), 1)
        self.assertEqual(len(svc.list_records(tenant_id="tenant-b")), 0)
        self.assertIsNone(svc.store.get_artifact(art_a.artifact_id, tenant_id="tenant-b"))
        self.assertIsNone(
            svc.store.find_artifact_by_checksum(art_a.checksum, tenant_id="tenant-b")
        )

    def test_dedupe_survives_restart(self):
        store1 = self._store()
        svc1 = build_acquisition_runtime(store=store1)
        svc1.register_source(_source())
        text = "sku,price\nX,2\n"
        a1 = svc1.ingest_text(
            source_id="src-1", tenant_id="tenant-a", text=text, content_type="text/csv"
        )
        a2 = svc1.ingest_text(
            source_id="src-1", tenant_id="tenant-a", text=text, content_type="text/csv"
        )
        self.assertEqual(a1.artifact_id, a2.artifact_id)
        recs = svc1.parse(a1)
        fp = recs[0].fingerprint
        store1.close()

        store2 = self._store()
        svc2 = build_acquisition_runtime(store=store2)
        a3 = svc2.ingest_text(
            source_id="src-1", tenant_id="tenant-a", text=text, content_type="text/csv"
        )
        self.assertEqual(a3.artifact_id, a1.artifact_id)
        # Original fingerprint still present after restart (dedupe index)
        found = svc2.store.find_record_by_fingerprint(fp, tenant_id="tenant-a")
        self.assertIsNotNone(found)
        self.assertEqual(found.record_id, recs[0].record_id)


class AcquisitionProductionWiringTests(unittest.TestCase):
    def test_composition_exposes_acquisition_with_same_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "side.db")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "GITHUB_WRITE_ENABLED": "false",
                    "ACQUISITION_USE_SHARED_DB": "true",
                },
            )
            try:
                self.assertIsNotNone(runtime.acquisition_runtime)
                self.assertIsNotNone(runtime.tool_gateway)
                svc = runtime.acquisition_runtime.service
                self.assertIs(svc.gateway, runtime.tool_gateway)
                self.assertIs(svc.manager.gateway, runtime.tool_gateway)
                self.assertIsInstance(runtime.tool_gateway, ToolGateway)
                # Durable when shared side-effect sqlite present
                self.assertEqual(
                    runtime.acquisition_runtime.health().get("persistence_backend"),
                    "sqlite",
                )
                # Scheduler shared with workflow when available
                if runtime.workflow_runtime is not None:
                    self.assertIs(
                        svc.scheduler.scheduler,
                        runtime.workflow_runtime.scheduler,
                    )
                # Engine exposes acquisition
                self.assertIs(
                    runtime.workflow_engine.acquisition_service,
                    svc,
                )
            finally:
                runtime.close()

    def test_production_restart_keeps_acquisition_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "side.db")
            env = {
                "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                "SIDE_EFFECT_DB_PATH": path,
                "GITHUB_WRITE_ENABLED": "false",
                "ACQUISITION_USE_SHARED_DB": "true",
            }
            runtime_a = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                svc = runtime_a.acquisition_runtime.service
                svc.register_source(_source())
                art = svc.ingest_text(
                    source_id="src-1",
                    tenant_id="tenant-a",
                    text=f"sku,ean,name,price,currency\nS1,{VALID_EAN},W,9,USD\n",
                    content_type="text/csv",
                )
                records = svc.parse(art)
                rec_id = records[0].record_id
                art_id = art.artifact_id
            finally:
                runtime_a.close()

            runtime_b = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                svc_b = runtime_b.acquisition_runtime.service
                self.assertIsNotNone(
                    svc_b.store.get_artifact(art_id, tenant_id="tenant-a")
                )
                self.assertIsNotNone(svc_b.get_record(rec_id, tenant_id="tenant-a"))
                self.assertEqual(
                    svc_b.get_source("src-1", tenant_id="tenant-a").source_id, "src-1"
                )
                # No direct networking — manager always uses gateway
                self.assertIs(svc_b.manager.gateway, runtime_b.tool_gateway)
            finally:
                runtime_b.close()

    def test_schedule_uses_workflow_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "side.db")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "GITHUB_WRITE_ENABLED": "false",
                },
            )
            try:
                svc = runtime.acquisition_runtime.service
                svc.register_source(_source())
                state = svc.schedule_refresh(
                    schedule_id="acq-sched-1",
                    source_id="src-1",
                    tenant_id="tenant-a",
                    interval_seconds=60,
                    target="https://example.com/feed",
                )
                self.assertEqual(state.workflow_type, "acquisition.refresh")
                if runtime.workflow_runtime is not None:
                    found = runtime.workflow_runtime.scheduler.store.get("acq-sched-1")
                    self.assertIsNotNone(found)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
