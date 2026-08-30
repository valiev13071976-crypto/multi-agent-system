"""Production Runtime / Scale (3.12–3.41) — workload, drain, DLQ, HA, canary."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from agents.routing_health_store import SqliteRoutingHealthStore
from agents.routing_state_scope import (
    STATE_SCOPE_SHARED,
    routing_coordination_capabilities,
)
from evals.activation import RoutingActivationService
from evals.canary import CanaryController
from evals.promotion import STAGE_PRODUCTION_ELIGIBLE, CandidatePolicy
from evals.shadow import ShadowRunner
from evals.versions import ROUTING_POLICY_VERSION
from providers.governor import (
    GovernorLimits,
    ProviderGovernor,
    SqliteProviderGovernorStore,
)
from runtime.alerts import AlertThresholds, evaluate_alert_conditions
from runtime.capacity_snapshot import build_capacity_snapshot
from side_effects.errors import SideEffectPersistenceUnavailableError
from side_effects.persistence import build_side_effect_persistence
from task_queue.errors import QueueTenantOwnershipError
from task_queue.lanes import (
    LANE_BACKGROUND,
    LANE_BULK,
    LANE_INTERACTIVE,
    WORKLOAD_BACKGROUND,
    WORKLOAD_BATCH,
    WORKLOAD_INTERACTIVE,
    WORKLOAD_NORMAL,
    classify_workload,
    normalize_lane,
    route_heavy_job,
)
from task_queue.models import STATUS_DEAD_LETTERED, STATUS_QUEUED
from task_queue.pools import POOL_BATCH, POOL_INTERACTIVE, pool_for_lane
from task_queue.queue import TaskQueue
from task_queue.store import InMemoryTaskQueueStore
from task_queue.worker import TaskWorker, WorkerConfig


def _eligible_candidate(cid: str = "cand-a") -> CandidatePolicy:
    return CandidatePolicy(
        candidate_id=cid,
        candidate_version="1",
        base_routing_policy_version=ROUTING_POLICY_VERSION,
        proposed_routing_policy_version=ROUTING_POLICY_VERSION,
        stage=STAGE_PRODUCTION_ELIGIBLE,
        eval_suite_id="core",
        eval_suite_version="1",
        eval_run_id="run-1",
        eval_manifest_hash="h1",
        model_profile_version=ROUTING_POLICY_VERSION,
        production_eligible=True,
        production_active=False,
    )


class WorkloadClassificationTests(unittest.TestCase):
    def test_classify_interactive_normal_batch_background(self):
        ix = classify_workload(priority="high")
        self.assertEqual(ix.name, WORKLOAD_INTERACTIVE)
        self.assertEqual(ix.lane, LANE_INTERACTIVE)

        normal = classify_workload(priority="normal")
        self.assertEqual(normal.name, WORKLOAD_NORMAL)
        self.assertEqual(normal.lane, LANE_BACKGROUND)

        batch = classify_workload(
            metadata={"workload_class": "batch"},
        )
        self.assertEqual(batch.name, WORKLOAD_BATCH)
        self.assertEqual(batch.lane, LANE_BULK)

        bg = classify_workload(priority="low")
        self.assertEqual(bg.name, WORKLOAD_BACKGROUND)

    def test_heavy_excel_crawler_to_batch(self):
        self.assertEqual(route_heavy_job("excel_large"), LANE_BULK)
        self.assertEqual(route_heavy_job("crawler"), LANE_BULK)
        excel = classify_workload(job_type="excel_large")
        self.assertEqual(excel.name, WORKLOAD_BATCH)
        crawler = classify_workload(
            metadata={"trusted_job_type": "crawler"},
        )
        self.assertEqual(crawler.name, WORKLOAD_BATCH)

    def test_size_thresholds_to_batch(self):
        big = classify_workload(estimated_rows=200_000)
        self.assertEqual(big.name, WORKLOAD_BATCH)
        big_b = classify_workload(estimated_bytes=60 * 1024 * 1024)
        self.assertEqual(big_b.name, WORKLOAD_BATCH)

    def test_normal_alias_lane(self):
        self.assertEqual(normalize_lane("normal"), LANE_BACKGROUND)
        self.assertEqual(pool_for_lane(LANE_BULK), POOL_BATCH)
        self.assertEqual(pool_for_lane(LANE_INTERACTIVE), POOL_INTERACTIVE)


class WorkerPoolIsolationTests(unittest.TestCase):
    def test_pool_cannot_claim_forbidden_lane(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        worker = TaskWorker(
            q,
            config=WorkerConfig(
                allowed_lanes=frozenset({LANE_INTERACTIVE}),
                pool_name=POOL_INTERACTIVE,
            ),
        )
        q.enqueue(
            workflow_id="bg",
            task_id="t",
            execution_key="ek-bg-forbid",
            tenant_id="t1",
            execution_lane=LANE_BACKGROUND,
        )
        claimed = q.dequeue(worker_id=worker.worker_id)
        self.assertIsNone(claimed)

    def test_drain_stops_claims(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        worker = TaskWorker(q, config=WorkerConfig())
        q.enqueue(
            workflow_id="w",
            task_id="t",
            execution_key="ek-drain",
            tenant_id="t1",
            execution_lane=LANE_BACKGROUND,
        )
        worker.begin_drain()
        self.assertTrue(worker.is_draining)
        result = asyncio.run(worker.run_once())
        self.assertIsNone(result)
        # Direct dequeue still works (drain is worker-side); reclaim path unaffected.
        self.assertIsNotNone(q.dequeue(worker_id="other"))


class InteractiveProtectionTests(unittest.TestCase):
    def test_interactive_protected_under_bulk_flood(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "flood.sqlite3")
            bundle = build_side_effect_persistence(
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                    "MAX_RUNNING_GLOBAL": "10",
                    "INTERACTIVE_RESERVED": "3",
                },
                durable=True,
                run_recovery_scan=False,
            )
            from task_queue.lanes import LaneCapacityConfig

            q = TaskQueue(
                store=bundle.task_queue_store,
                lease_seconds=60,
                lane_config=LaneCapacityConfig(
                    interactive_reserved=3, background_may_borrow=False
                ),
            )
            for i in range(30):
                q.enqueue(
                    workflow_id=f"bulk-{i}",
                    task_id=f"tb-{i}",
                    execution_key=f"ek-bulk-{i}",
                    tenant_id="tenant-bulk",
                    execution_lane=LANE_BULK,
                    priority="low",
                )
            for i in range(10):
                t = q.dequeue(
                    worker_id=f"wb{i}",
                    max_running_global=10,
                    max_running_per_tenant=50,
                )
                if t is None:
                    break
            q.enqueue(
                workflow_id="ix",
                task_id="tix",
                execution_key="ek-ix-flood",
                tenant_id="tenant-user",
                execution_lane=LANE_INTERACTIVE,
                priority="high",
            )
            ix = q.dequeue(
                worker_id="wix", max_running_global=10, max_running_per_tenant=50
            )
            self.assertIsNotNone(ix)
            self.assertEqual(ix.execution_lane, LANE_INTERACTIVE)
            if bundle.connection is not None:
                bundle.connection.close()


class DlqRedriveTests(unittest.TestCase):
    def test_redrive_tenant_fail_closed_and_success(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        task = q.enqueue(
            workflow_id="wf",
            task_id="t",
            execution_key="ek-dlq",
            tenant_id="tenant-a",
            metadata={"idempotency_key": "idem-1"},
        )
        leased = q.dequeue(worker_id="w0")
        assert leased is not None
        q.start(leased.queue_task_id, leased.lease_id, worker_id="w0")
        lettered = q.dead_letter(
            leased.queue_task_id,
            leased.lease_id,
            error_code="permanent_failure",
            worker_id="w0",
        )
        self.assertEqual(lettered.status, STATUS_DEAD_LETTERED)
        with self.assertRaises(QueueTenantOwnershipError):
            q.redrive_dead_letter(
                lettered.queue_task_id,
                actor_ref="ops",
                tenant_id="tenant-other",
            )
        redriven = q.redrive_dead_letter(
            lettered.queue_task_id,
            actor_ref="ops",
            tenant_id="tenant-a",
        )
        self.assertEqual(redriven.status, STATUS_QUEUED)
        self.assertEqual(redriven.execution_key, "ek-dlq")
        self.assertEqual(redriven.metadata.get("idempotency_key"), "idem-1")
        self.assertIsNone(redriven.lease_id)
        self.assertGreater(redriven.attempt, lettered.attempt)


class CapacityAndAlertTests(unittest.TestCase):
    def test_capacity_snapshot_fields(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        q.enqueue(
            workflow_id="a",
            task_id="t",
            execution_key="ek-cap",
            tenant_id="t1",
            execution_lane=LANE_INTERACTIVE,
        )
        snap = build_capacity_snapshot(q)
        d = snap.as_dict()
        self.assertIn("queue_depth_by_lane", d)
        self.assertIn("active_workers", d)
        self.assertIn("saturated_pools", d)
        self.assertIn("oldest_queued_age_seconds", d)
        self.assertIn("rejection_counts", d)
        self.assertIn("utilization", d)
        self.assertGreaterEqual(d["queue_depth_by_lane"].get(LANE_INTERACTIVE, 0), 1)

    def test_alert_conditions(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        for i in range(5):
            q.enqueue(
                workflow_id=f"w{i}",
                task_id=f"t{i}",
                execution_key=f"ek-al-{i}",
                tenant_id="t1",
                execution_lane=LANE_BACKGROUND,
            )
        snap = build_capacity_snapshot(
            q, admission_metrics={"overload_reject": 50}
        )
        # Force dlq depth via metadata walk — enqueue then dead-letter one.
        t = q.enqueue(
            workflow_id="dlq",
            task_id="td",
            execution_key="ek-al-dlq",
            tenant_id="t1",
        )
        leased = q.dequeue(worker_id="w")
        assert leased is not None
        q.start(leased.queue_task_id, leased.lease_id, worker_id="w")
        q.dead_letter(
            leased.queue_task_id,
            leased.lease_id,
            error_code="x",
            worker_id="w",
        )
        snap2 = build_capacity_snapshot(
            q, admission_metrics={"overload_reject": 50}
        )
        alerts = evaluate_alert_conditions(
            snap2,
            AlertThresholds(
                queue_depth_high=1,
                overload_reject_high=10,
                dlq_depth_high=1,
                oldest_job_age_seconds=0.0,
            ),
            worker_healthy=False,
            governor_available=False,
        )
        codes = {a.code for a in alerts}
        self.assertIn("queue_depth_high", codes)
        self.assertIn("overload_repeated", codes)
        self.assertIn("dlq_growth", codes)
        self.assertIn("worker_unhealthy", codes)
        self.assertIn("governor_unavailable", codes)
        self.assertIn("oldest_job_age_high", codes)


class ShadowCanaryTests(unittest.TestCase):
    def test_shadow_no_side_effect_flag(self):
        runner = ShadowRunner()
        ev = runner.run(
            {"candidate_id": "c1"},
            "input-ref-1",
            tenant_id="tenant-a",
            cost_observable=True,
        )
        self.assertFalse(ev.side_effects)
        self.assertFalse(ev.mutates_user_response)
        self.assertFalse(ev.changes_routing)
        self.assertTrue(ev.cost_observable)

    def test_canary_assign_deterministic_and_disable(self):
        ctrl = CanaryController()
        ctrl.enable("cand-1", 50, "policy-v1")
        a = ctrl.assign("req-42")
        ctrl2 = CanaryController()
        ctrl2.enable("cand-1", 50, "policy-v1")
        self.assertEqual(ctrl2.assign("req-42"), a)
        ctrl.disable()
        self.assertFalse(ctrl.assign("req-42"))
        self.assertFalse(ctrl.enabled)

    def test_canary_assign_stable(self):
        ctrl = CanaryController()
        ctrl.enable("cand-1", 100, "pv")
        self.assertTrue(ctrl.assign("any-req"))
        ctrl.disable()
        self.assertFalse(ctrl.assign("any-req"))

    def test_canary_rollback_via_activation(self):
        svc = RoutingActivationService(max_candidate_age=timedelta(days=30))
        svc.activate(
            _eligible_candidate("cand-old"),
            actor_ref="ops",
            expected_policy_version=ROUTING_POLICY_VERSION,
        )
        svc.activate(
            _eligible_candidate("cand-new"),
            actor_ref="ops",
            expected_policy_version=ROUTING_POLICY_VERSION,
        )
        self.assertEqual(svc.active_candidate_id, "cand-new")
        ctrl = CanaryController(activation=svc)
        ctrl.enable("cand-new", 10, ROUTING_POLICY_VERSION)
        restored = ctrl.rollback("ops")
        self.assertFalse(ctrl.enabled)
        self.assertEqual(svc.active_candidate_id, "cand-old")
        self.assertIsNotNone(restored)


class SharedHealthAndHaTests(unittest.TestCase):
    def test_shared_health_store_round_trip(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "health.sqlite3")
            store = SqliteRoutingHealthStore(path)
            store.record_failure("openai", "gpt", error_class="TimeoutError")
            store.record_success("openai", "gpt")
            snap = store.snapshot("openai", "gpt")
            self.assertEqual(snap.state, "healthy")
            all_rows = store.get_snapshot()
            self.assertIn("openai:gpt", all_rows)
            caps = routing_coordination_capabilities(health_tracker=store)
            self.assertEqual(caps["routing_health_scope"], STATE_SCOPE_SHARED)
            self.assertTrue(caps["multi_worker_shared_routing_health_ready"])
            self.assertEqual(caps["routing_health_shared_backing"], "available")

    def test_two_workers_claim_distinct_tasks(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "q.sqlite3")
            bundle = build_side_effect_persistence(
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                    "MAX_RUNNING_GLOBAL": "50",
                },
                durable=True,
                run_recovery_scan=False,
            )
            q = TaskQueue(store=bundle.task_queue_store, lease_seconds=60)
            q.enqueue(
                workflow_id="w1",
                task_id="t1",
                execution_key="ek-ha-1",
                tenant_id="t1",
            )
            q.enqueue(
                workflow_id="w2",
                task_id="t2",
                execution_key="ek-ha-2",
                tenant_id="t1",
            )
            a = q.dequeue(worker_id="w-a", max_running_global=50)
            b = q.dequeue(worker_id="w-b", max_running_global=50)
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            self.assertNotEqual(a.queue_task_id, b.queue_task_id)
            if bundle.connection is not None:
                bundle.connection.close()

    def test_governor_unavailable_fail_closed_smoke(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "gov.sqlite3")
            limits = GovernorLimits(max_concurrency=2)
            store = SqliteProviderGovernorStore(path, limits)
            gov = ProviderGovernor(store=store, limits=limits)
            slot = gov.acquire(provider_id="p", model_id="m")
            gov.release(slot)
            import sqlite3
            from unittest.mock import patch

            with patch.object(store, "_tx", side_effect=sqlite3.Error("disk I/O error")):
                with self.assertRaises(SideEffectPersistenceUnavailableError) as ctx:
                    store.acquire(
                        provider_id="p",
                        model_id="m",
                        lane="background",
                        worker_id="w",
                    )
            self.assertIn("provider_governor_unavailable", str(ctx.exception))

    def test_worker_health_dict(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        w = TaskWorker(q, config=WorkerConfig(pool_name="normal"))
        h = w.health()
        self.assertIn("liveness", h)
        self.assertIn("readiness", h)
        self.assertIn("draining", h)
        self.assertIn("saturation", h)
        self.assertIn("persistence_ok", h)
        w.begin_drain()
        self.assertEqual(w.health()["draining"], True)
        self.assertEqual(w.health()["readiness"], "not_ready")


class E2ERuntimeFlowTests(unittest.TestCase):
    def test_request_to_completion_metrics_path(self):
        """request → classify → enqueue → claim → complete (+ interactive vs heavy)."""
        from runtime.metrics import RUNTIME_COUNTERS
        from task_queue.models import STATUS_COMPLETED

        before = RUNTIME_COUNTERS.total("enqueue")
        ix = classify_workload(priority="critical")
        heavy = classify_workload(job_type="content_generation")
        self.assertEqual(ix.lane, LANE_INTERACTIVE)
        self.assertEqual(heavy.name, WORKLOAD_BACKGROUND)

        q = TaskQueue(store=InMemoryTaskQueueStore())
        q.enqueue(
            workflow_id="e2e-ix",
            task_id="t1",
            execution_key="ek-e2e-ix",
            tenant_id="tenant-e2e",
            execution_lane=ix.lane,
            priority="critical",
        )
        q.enqueue(
            workflow_id="e2e-heavy",
            task_id="t2",
            execution_key="ek-e2e-heavy",
            tenant_id="tenant-e2e",
            execution_lane=heavy.lane,
            priority="low",
        )
        w_ix = TaskWorker(
            q,
            config=WorkerConfig(
                allowed_lanes=frozenset({LANE_INTERACTIVE}),
                pool_name=POOL_INTERACTIVE,
            ),
        )
        claimed = q.dequeue(worker_id=w_ix.worker_id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.execution_lane, LANE_INTERACTIVE)
        q.start(claimed.queue_task_id, claimed.lease_id, worker_id=w_ix.worker_id)
        done = q.ack(
            claimed.queue_task_id, claimed.lease_id, worker_id=w_ix.worker_id
        )
        self.assertEqual(done.status, STATUS_COMPLETED)

        w_bg = TaskWorker(
            q,
            config=WorkerConfig(
                allowed_lanes=frozenset({LANE_BACKGROUND, LANE_BULK}),
                pool_name=POOL_BATCH,
            ),
        )
        heavy_claimed = q.dequeue(worker_id=w_bg.worker_id)
        self.assertIsNotNone(heavy_claimed)
        self.assertNotEqual(heavy_claimed.execution_lane, LANE_INTERACTIVE)
        self.assertGreaterEqual(RUNTIME_COUNTERS.total("enqueue"), before)

    def test_media_and_scraping_routing(self):
        self.assertEqual(route_heavy_job("media"), LANE_BACKGROUND)
        self.assertEqual(route_heavy_job("content_generation"), LANE_BACKGROUND)
        self.assertEqual(route_heavy_job("scraping"), LANE_BULK)
        media = classify_workload(job_type="media")
        self.assertEqual(media.name, WORKLOAD_BACKGROUND)


class LoadSheddingTests(unittest.TestCase):
    def test_tenant_batch_quota_rejects(self):
        from workflow.admission import (
            DECISION_REJECT,
            AdmissionController,
            AdmissionLimits,
        )

        ctl = AdmissionController(
            AdmissionLimits(
                max_pending_global=1000,
                max_pending_per_tenant=1000,
                max_batch_pending_per_tenant=1,
            )
        )
        q = TaskQueue(store=InMemoryTaskQueueStore())
        q.enqueue(
            workflow_id="b1",
            task_id="t1",
            execution_key="ek-batch-q1",
            tenant_id="tenant-a",
            execution_lane=LANE_BULK,
            priority="low",
        )
        decision = ctl.evaluate_enqueue(
            q,
            tenant_id="tenant-a",
            priority="low",
            execution_lane=LANE_BULK,
        )
        self.assertEqual(decision.decision, DECISION_REJECT)
        self.assertEqual(decision.reason_code, "tenant_quota")


if __name__ == "__main__":
    unittest.main()
