"""Production wiring for Excel / Data Intelligence — compose_side_effect_runtime."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from autonomy.capabilities import CAP_FILESYSTEM_READ, CAP_FILESYSTEM_WRITE, CapabilitySet
from autonomy.models import utc_now
from data_intel.large import LargeDatasetPolicy
from data_intel.store import SqliteDatasetStore
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets
from tools.models import ToolRequest


def _caps(*names):
    return CapabilitySet(subject_id="u1", capabilities=names, issued_at=utc_now())


def _durable_env(path: str) -> dict:
    return {
        "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
        "SIDE_EFFECT_DB_PATH": path,
        "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
        "DOCUMENT_BACKEND": "sqlite",
        "ACQUISITION_BACKEND": "sqlite",
        "ACQUISITION_USE_SHARED_DB": "true",
        "DATA_INTEL_BACKEND": "sqlite",
        "DATA_INTEL_USE_SHARED_DB": "true",
        "DATA_INTEL_ENABLED": "true",
    }


class DataIntelProductionWiringTests(unittest.IsolatedAsyncioTestCase):
    def _compose(self, tmp: str, name: str = "di.sqlite3"):
        path = str(Path(tmp) / name)
        runtime = compose_side_effect_runtime(
            secrets=DictSecrets(), env=_durable_env(path)
        )
        return runtime, path

    def test_compose_builds_data_intelligence_with_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self._compose(tmp)
            try:
                self.assertIsNotNone(runtime.data_intelligence_runtime)
                self.assertIsNotNone(runtime.data_intelligence_runtime.service)
                self.assertIsInstance(
                    runtime.data_intelligence_runtime.store, SqliteDatasetStore
                )
                self.assertEqual(
                    runtime.data_intelligence_runtime.store.persistence_backend, "sqlite"
                )
                self.assertIs(
                    runtime.workflow_engine.data_intelligence,
                    runtime.data_intelligence_runtime.service,
                )
            finally:
                runtime.close()

    def test_same_gateway_workflow_documents_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self._compose(tmp, "di2.sqlite3")
            try:
                di = runtime.data_intelligence_runtime.service
                self.assertIs(di.document_service, runtime.document_runtime.service)
                self.assertIs(di.workflow_runtime, runtime.workflow_runtime)
                self.assertIs(di.acquisition_service, runtime.acquisition_runtime.service)
                self.assertIs(
                    runtime.acquisition_runtime.service.gateway, runtime.tool_gateway
                )
            finally:
                runtime.close()

    def test_data_tools_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self._compose(tmp, "di3.sqlite3")
            try:
                for tool_id in (
                    "data.profile",
                    "data.normalize",
                    "data.search",
                    "data.match",
                    "data.compare",
                    "data.reconcile",
                    "data.aggregate",
                    "data.generate_excel",
                ):
                    desc = runtime.tool_registry.get(tool_id)
                    self.assertIsNotNone(desc, tool_id)
                    self.assertTrue(desc.enabled, tool_id)
            finally:
                runtime.close()

    async def test_search_and_generate_excel_via_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self._compose(tmp, "di4.sqlite3")
            try:
                svc = runtime.data_intelligence_runtime.service
                csv = b"INN,company_name,amount\n7707083893,OOO A,10\n"
                ing = svc.ingest(
                    csv, filename="p.csv", tenant_id="tenant-a", enqueue_large=False
                )
                ds = ing["dataset_id"]
                caps = _caps(CAP_FILESYSTEM_READ, CAP_FILESYSTEM_WRITE)
                search_req = ToolRequest(
                    request_id=str(uuid.uuid4()),
                    workflow_id="wf",
                    task_id="t1",
                    tool_id="data.search",
                    operation="search",
                    arguments={"dataset_id": ds, "inn": "7707083893"},
                    tenant_id="tenant-a",
                    requested_capabilities=(CAP_FILESYSTEM_READ,),
                )
                search_res = await runtime.tool_gateway.invoke(
                    search_req, capabilities=caps
                )
                self.assertTrue(search_res.success, search_res.error_code)
                self.assertGreaterEqual(int(search_res.data.get("total") or 0), 1)

                gen_req = ToolRequest(
                    request_id=str(uuid.uuid4()),
                    workflow_id="wf",
                    task_id="t2",
                    tool_id="data.generate_excel",
                    operation="generate_excel",
                    arguments={"dataset_id": ds, "kind": "payments"},
                    tenant_id="tenant-a",
                    requested_capabilities=(CAP_FILESYSTEM_WRITE,),
                )
                gen_res = await runtime.tool_gateway.invoke(gen_req, capabilities=caps)
                self.assertTrue(gen_res.success, gen_res.error_code)
                self.assertTrue(gen_res.data.get("content_b64"))
            finally:
                runtime.close()

    def test_large_process_registered_and_enqueues(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self._compose(tmp, "di5.sqlite3")
            try:
                defs = runtime.workflow_runtime.definitions
                self.assertIsNotNone(defs.get("data.large_process", "1"))
                svc = runtime.data_intelligence_runtime.service
                svc.large_policy = LargeDatasetPolicy(max_sync_rows=5, rows_per_batch=3)
                lines = ["INN,amount"] + [f"7707083893,{i}" for i in range(12)]
                ing = svc.ingest(
                    ("\n".join(lines)).encode(),
                    filename="big.csv",
                    tenant_id="tenant-a",
                    enqueue_large=True,
                )
                self.assertTrue(ing["async"])
                self.assertIsNotNone(ing["workflow_id"])
                st = runtime.workflow_runtime.state_manager.get(ing["workflow_id"])
                self.assertEqual(st.workflow_type, "data.large_process")
                queue_tasks = list(runtime.workflow_runtime.queue.store.list_all())
                self.assertTrue(
                    any(t.workflow_id == ing["workflow_id"] for t in queue_tasks)
                    or st.status in {"queued", "running", "planned"}
                )
            finally:
                runtime.close()

    def test_restart_preserves_dataset_and_partials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "di6.sqlite3")
            env = _durable_env(path)
            runtime_a = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                svc_a = runtime_a.data_intelligence_runtime.service
                csv = b"INN,amount\n7707083893,42\n"
                ing = svc_a.ingest(
                    csv, filename="r.csv", tenant_id="tenant-a", enqueue_large=False
                )
                ds = ing["dataset_id"]
                svc_a.store.save_partial(
                    ds, 0, {"status": "completed", "row_count": 1}, tenant_id="tenant-a"
                )
            finally:
                runtime_a.close()

            runtime_b = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                svc_b = runtime_b.data_intelligence_runtime.service
                desc = svc_b.store.get_dataset(ds, tenant_id="tenant-a")
                self.assertIsNotNone(desc)
                rows = svc_b.store.get_rows(ds, tenant_id="tenant-a")
                self.assertGreaterEqual(len(rows), 1)
                partials = svc_b.store.list_partials(ds, tenant_id="tenant-a")
                self.assertIn(0, partials)
                self.assertEqual(partials[0]["status"], "completed")
            finally:
                runtime_b.close()

    def test_tenant_isolation_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self._compose(tmp, "di7.sqlite3")
            try:
                svc = runtime.data_intelligence_runtime.service
                ing = svc.ingest(
                    b"INN,amount\n7707083893,1\n",
                    filename="t.csv",
                    tenant_id="tenant-a",
                    enqueue_large=False,
                )
                self.assertIsNone(
                    svc.store.get_dataset(ing["dataset_id"], tenant_id="tenant-b")
                )
            finally:
                runtime.close()

    def test_no_second_gateway_or_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self._compose(tmp, "di8.sqlite3")
            try:
                self.assertIs(
                    runtime.acquisition_runtime.service.gateway, runtime.tool_gateway
                )
                self.assertIs(
                    runtime.data_intelligence_runtime.service.workflow_runtime,
                    runtime.workflow_runtime,
                )
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
