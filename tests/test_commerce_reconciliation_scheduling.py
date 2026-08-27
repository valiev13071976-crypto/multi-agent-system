"""Commerce reconciliation scheduling — production wiring tests."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from commerce.contracts import CommerceOrder, CommerceOrderLine, InventoryPosition
from commerce.service import CommerceService
from commerce.states import MARKING_WITHDRAWN, ORDER_COMPLETED, ORDER_SHIPMENT
from commerce.store import CommerceStore
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets
from workflow.models import utc_now


def _env(path: str, **extra) -> dict:
    base = {
        "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
        "SIDE_EFFECT_DB_PATH": path,
        "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
        "COMMERCE_ENABLED": "true",
        "COMMERCE_USE_SHARED_DB": "true",
        "INTEGRATION_SECRETS_BACKEND": "memory",
    }
    base.update(extra)
    return base


def _seed_order(svc: CommerceService, tenant: str, order_id: str | None = None) -> str:
    oid = order_id or f"ord-{uuid.uuid4().hex[:8]}"
    svc.inventory.seed(
        InventoryPosition(
            product_ref="sku-1",
            warehouse="main",
            available=5,
            fetched_at=datetime.now(timezone.utc),
        ),
        tenant_id=tenant,
    )
    svc.marking.seed(tenant_id=tenant, code_ref="code-1")
    order = CommerceOrder(
        order_id=oid,
        tenant_id=tenant,
        buyer_type="B2C",
        buyer_ref="buyer-1",
        lines=(
            CommerceOrderLine(
                product_ref="sku-1",
                quantity=1,
                warehouse="main",
                marking_code_refs=("code-1",),
            ),
        ),
        fulfillment_state=ORDER_SHIPMENT,
        scenario="b2c_fulfillment",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    from commerce.service import _order_payload

    svc.store.save_order(tenant, oid, _order_payload(order), ORDER_SHIPMENT)
    return oid


class FakeHitl:
    def __init__(self):
        self.calls: list = []

    def request_approval(self, action, decision, *, requested_by, now=None):
        rec = SimpleNamespace(approval_id=f"appr-{uuid.uuid4().hex[:8]}")
        self.calls.append(
            {
                "action": action,
                "decision": decision,
                "requested_by": requested_by,
                "approval_id": rec.approval_id,
            }
        )
        return rec


class CommerceReconciliationSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def test_compose_scheduling_enabled_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "c1.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env=_env(
                    path,
                    COMMERCE_RECONCILIATION_ENABLED="true",
                    COMMERCE_RECONCILIATION_INTERVAL_SECONDS="120",
                    COMMERCE_RECONCILIATION_TENANTS="tenant-a,tenant-b",
                ),
            )
            try:
                cr = runtime.commerce_runtime
                self.assertIsNotNone(cr)
                self.assertTrue(cr.reconciliation_enabled)
                self.assertEqual(cr.reconciliation_interval_seconds, 120.0)
                health = cr.health()
                self.assertTrue(health["reconciliation_enabled"])
                self.assertEqual(health["reconciliation_schedules"], 2)
                # same WorkflowScheduler object
                self.assertIs(
                    cr.reconciliation_scheduler.scheduler,
                    runtime.workflow_runtime.scheduler,
                )
                ids = {
                    s.schedule_id
                    for s in runtime.workflow_runtime.scheduler.store.list_all()
                    if s.workflow_type == "commerce.reconcile"
                }
                self.assertEqual(
                    ids, {"commerce-reconcile:tenant-a", "commerce-reconcile:tenant-b"}
                )
            finally:
                runtime.close()

    def test_safe_default_scheduling_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "c0.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(), env=_env(path)
            )
            try:
                cr = runtime.commerce_runtime
                self.assertIsNotNone(cr)
                self.assertFalse(cr.reconciliation_enabled)
                self.assertEqual(cr.health()["reconciliation_schedules"], 0)
            finally:
                runtime.close()

    async def test_tick_creates_per_tenant_reconcile_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "c2.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env=_env(
                    path,
                    COMMERCE_RECONCILIATION_ENABLED="true",
                    COMMERCE_RECONCILIATION_INTERVAL_SECONDS="3600",
                    COMMERCE_RECONCILIATION_TENANTS="tenant-a,tenant-b",
                ),
            )
            try:
                wr = runtime.workflow_runtime
                # force due immediately
                now = utc_now()
                for sid in (
                    "commerce-reconcile:tenant-a",
                    "commerce-reconcile:tenant-b",
                ):
                    st = wr.scheduler.store.get(sid)
                    wr.scheduler.store.save(
                        replace(st, next_run_at=now - timedelta(seconds=1))
                    )

                launched = await wr.tick_schedules()
                self.assertEqual(len(launched), 2)
                states = [wr.state_manager.get(wid) for wid in launched]
                tenants = {dict(s.metadata).get("tenant_id") for s in states}
                self.assertEqual(tenants, {"tenant-a", "tenant-b"})
                for s in states:
                    self.assertEqual(
                        dict(s.metadata).get("trigger")
                        or s.metadata.get("trigger"),
                        "scheduled",
                    )
                    self.assertTrue(
                        str(s.execution_key).startswith("commerce-reconcile:")
                    )

                # same schedule window: reset next_run to prior window → idempotent
                keys_before = {s.execution_key for s in states}
                for s in states:
                    tenant = dict(s.metadata).get("tenant_id")
                    sid = f"commerce-reconcile:{tenant}"
                    window = int(str(s.execution_key).rsplit(":", 1)[-1])
                    st = wr.scheduler.store.get(sid)
                    wr.scheduler.store.save(
                        replace(
                            st,
                            next_run_at=datetime.fromtimestamp(
                                window, tz=timezone.utc
                            ),
                        )
                    )

                launched2 = await wr.tick_schedules()
                self.assertEqual(len(launched2), 2)
                self.assertEqual(set(launched2), set(launched))

                # next interval → new runs (due now, different window than prior keys)
                prior_windows = {
                    int(str(k).rsplit(":", 1)[-1]) for k in keys_before
                }
                new_run = utc_now() - timedelta(seconds=5)
                while int(new_run.timestamp()) in prior_windows:
                    new_run -= timedelta(seconds=1)
                for sid in (
                    "commerce-reconcile:tenant-a",
                    "commerce-reconcile:tenant-b",
                ):
                    st = wr.scheduler.store.get(sid)
                    wr.scheduler.store.save(replace(st, next_run_at=new_run))
                launched3 = await wr.tick_schedules()
                self.assertEqual(len(launched3), 2)
                self.assertTrue(set(launched3).isdisjoint(set(launched)))
                for wid in launched3:
                    key = wr.state_manager.get(wid).execution_key
                    self.assertNotIn(key, keys_before)
            finally:
                runtime.close()

    async def test_restart_does_not_duplicate_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "c3.sqlite3")
            env = _env(
                path,
                COMMERCE_RECONCILIATION_ENABLED="true",
                COMMERCE_RECONCILIATION_INTERVAL_SECONDS="3600",
                COMMERCE_RECONCILIATION_TENANTS="tenant-a",
            )
            runtime = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                wr = runtime.workflow_runtime
                now = utc_now()
                window = int(now.timestamp())
                st = wr.scheduler.store.get("commerce-reconcile:tenant-a")
                wr.scheduler.store.save(
                    replace(
                        st,
                        next_run_at=datetime.fromtimestamp(window, tz=timezone.utc),
                    )
                )
                launched = await wr.tick_schedules()
                self.assertEqual(len(launched), 1)
                wid = launched[0]
                key = wr.state_manager.get(wid).execution_key
            finally:
                runtime.close()

            runtime2 = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                wr2 = runtime2.workflow_runtime
                # reconstruct schedule with same window (missed/eligible)
                st2 = wr2.scheduler.store.get("commerce-reconcile:tenant-a")
                wr2.scheduler.store.save(
                    replace(
                        st2,
                        next_run_at=datetime.fromtimestamp(window, tz=timezone.utc),
                    )
                )
                launched2 = await wr2.tick_schedules()
                self.assertEqual(len(launched2), 1)
                self.assertEqual(launched2[0], wid)
                again = wr2.state_manager.find_by_execution_key(
                    key, tenant_id="tenant-a"
                )
                self.assertIsNotNone(again)
                self.assertEqual(again.workflow_id, wid)
            finally:
                runtime2.close()

    async def test_tick_runs_engine_and_persists_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "c4.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env=_env(
                    path,
                    COMMERCE_RECONCILIATION_ENABLED="true",
                    COMMERCE_RECONCILIATION_TENANTS="tenant-a",
                ),
            )
            try:
                svc = runtime.commerce_runtime.service
                oid = _seed_order(svc, "tenant-a")
                wr = runtime.workflow_runtime
                st = wr.scheduler.store.get("commerce-reconcile:tenant-a")
                wr.scheduler.store.save(
                    replace(st, next_run_at=utc_now() - timedelta(seconds=1))
                )
                launched = await wr.tick_schedules()
                self.assertEqual(len(launched), 1)
                task = await wr.worker.run_once()
                self.assertIsNotNone(task)
                findings = svc.store.list_reconcile("tenant-a")
                self.assertTrue(findings)
                row = findings[0]
                self.assertIn(row["status"], {
                    "OK",
                    "WARNING",
                    "RECONCILIATION_ERROR",
                    "HUMAN_REVIEW",
                })
                self.assertTrue(row.get("run_id") or row.get("workflow_id"))
                self.assertTrue(row.get("checked_at"))
                # cross-tenant inaccessible
                self.assertEqual(svc.store.list_reconcile("tenant-b"), [])
                self.assertIsNone(
                    svc.store.get_reconcile_finding("tenant-b", row["finding_id"])
                )
            finally:
                runtime.close()

    def test_event_triggered_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "c5.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env=_env(path, COMMERCE_RECONCILIATION_ENABLED="false"),
            )
            try:
                svc = runtime.commerce_runtime.service
                oid = _seed_order(svc, "tenant-a")
                # complete-ish: force completed then enqueue via hook API
                out = svc.enqueue_reconcile(
                    "tenant-a", order_id=oid, reason="b2c_completed"
                )
                self.assertIn("execution_key", out)
                self.assertFalse(out.get("idempotent"))
                again = svc.enqueue_reconcile(
                    "tenant-a", order_id=oid, reason="b2c_completed"
                )
                self.assertTrue(again.get("idempotent"))
                self.assertEqual(again["workflow_id"], out["workflow_id"])
            finally:
                runtime.close()

    def test_human_review_hitl_no_auto_correct(self):
        store = CommerceStore(path=":memory:")
        hitl = FakeHitl()
        svc = CommerceService(store=store, hitl_service=hitl)
        tenant = "tenant-a"
        oid = f"ord-{uuid.uuid4().hex[:8]}"
        svc.inventory.seed(
            InventoryPosition(
                product_ref="sku-1",
                warehouse="main",
                available=1,
                fetched_at=datetime.now(timezone.utc),
            ),
            tenant_id=tenant,
        )
        # withdrawn while cancelled → HUMAN_REVIEW
        svc.marking.seed(tenant_id=tenant, code_ref="code-x", status=MARKING_WITHDRAWN)
        order = CommerceOrder(
            order_id=oid,
            tenant_id=tenant,
            buyer_type="B2C",
            buyer_ref="b",
            lines=(
                CommerceOrderLine(
                    product_ref="sku-1",
                    quantity=1,
                    warehouse="main",
                    marking_code_refs=("code-x",),
                ),
            ),
            fulfillment_state="CANCELLED",
            scenario="b2c_fulfillment",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        from commerce.service import _order_payload
        from commerce.states import ORDER_CANCELLED

        svc.store.save_order(tenant, oid, _order_payload(order), ORDER_CANCELLED)
        result = svc.reconcile_order(tenant, oid, workflow_id="wf-recon-1", run_id="run-1")
        self.assertEqual(result["status"], "HUMAN_REVIEW")
        self.assertFalse(result["auto_corrected"])
        self.assertTrue(hitl.calls)
        self.assertEqual(hitl.calls[0]["requested_by"], "commerce.reconcile")
        self.assertFalse(hitl.calls[0]["action"].metadata.get("auto_correct"))
        # marking untouched
        st = svc.marking.read_status(tenant_id=tenant, code_ref="code-x")
        self.assertEqual(st.status, MARKING_WITHDRAWN)
        persisted = store.get_reconcile_finding(tenant, result["finding_id"])
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["status"], "HUMAN_REVIEW")
        self.assertFalse(persisted.get("auto_corrected"))


if __name__ == "__main__":
    unittest.main()
