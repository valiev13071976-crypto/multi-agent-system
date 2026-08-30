"""Phase 3 Block 2 — lanes, fairness, aging, provider governor."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from agents.model_router import ModelRouter
from agents.routing_audit import REJECT_CAPACITY_UNAVAILABLE
from providers.governor import (
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    GovernorLimits,
    InMemoryProviderGovernorStore,
    ProviderCapacityUnavailable,
    ProviderGovernor,
    SqliteProviderGovernorStore,
)
from side_effects.persistence import build_side_effect_persistence
from task_queue.lanes import (
    LANE_BACKGROUND,
    LANE_BULK,
    LANE_INTERACTIVE,
    LANE_SCHEDULED,
    LaneCapacityConfig,
    effective_priority_rank,
)
from task_queue.models import PRIORITY_LOW, PRIORITY_NORMAL, utc_now
from task_queue.queue import TaskQueue


def _env(path: str, **extra) -> dict:
    base = {
        "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
        "SIDE_EFFECT_DB_PATH": path,
        "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
        "MAX_PENDING_GLOBAL": "2000",
        "MAX_PENDING_PER_TENANT": "500",
        "MAX_RUNNING_GLOBAL": "20",
        "MAX_RUNNING_PER_TENANT": "50",
        "INTERACTIVE_RESERVED": "5",
        "BACKGROUND_MAY_BORROW_INTERACTIVE": "true",
        "PRIORITY_AGING_SECONDS": "1",
        "PRIORITY_AGING_MAX_BOOST": "2",
        "TENANT_FAIRNESS_ENABLED": "true",
    }
    base.update(extra)
    return base


class InteractiveIsolationTests(unittest.TestCase):
    def test_interactive_claimable_under_background_saturation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "iso.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path, MAX_RUNNING_GLOBAL="10", INTERACTIVE_RESERVED="3"),
                durable=True,
                run_recovery_scan=False,
            )
            q = TaskQueue(
                store=bundle.task_queue_store,
                lease_seconds=60,
                lane_config=LaneCapacityConfig(
                    interactive_reserved=3, background_may_borrow=False
                ),
            )
            # Fill background capacity to max_bg = 10-3 = 7
            for i in range(20):
                q.enqueue(
                    workflow_id=f"bg-{i}",
                    task_id=f"t-bg-{i}",
                    execution_key=f"ek-bg-{i}",
                    tenant_id="tenant-bg",
                    execution_lane=LANE_BACKGROUND,
                    priority=PRIORITY_LOW,
                )
            claimed_bg = []
            for i in range(10):
                t = q.dequeue(
                    worker_id=f"w{i}",
                    max_running_global=10,
                    max_running_per_tenant=50,
                )
                if t is None:
                    break
                claimed_bg.append(t)
            self.assertEqual(len(claimed_bg), 7)
            self.assertTrue(all(t.execution_lane == LANE_BACKGROUND for t in claimed_bg))

            # Interactive must still claim into reserved slots
            q.enqueue(
                workflow_id="ix-1",
                task_id="t-ix",
                execution_key="ek-ix",
                tenant_id="tenant-ix",
                execution_lane=LANE_INTERACTIVE,
                priority="high",
            )
            ix = q.dequeue(
                worker_id="w-ix", max_running_global=10, max_running_per_tenant=50
            )
            self.assertIsNotNone(ix)
            self.assertEqual(ix.execution_lane, LANE_INTERACTIVE)
            if bundle.connection is not None:
                bundle.connection.close()

    def test_bulk_cannot_steal_reservation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "steal.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(
                store=bundle.task_queue_store,
                lease_seconds=60,
                lane_config=LaneCapacityConfig(
                    interactive_reserved=2, background_may_borrow=False
                ),
            )
            for i in range(10):
                q.enqueue(
                    workflow_id=f"b-{i}",
                    task_id=f"t-{i}",
                    execution_key=f"ek-{i}",
                    tenant_id="tA",
                    execution_lane=LANE_BULK,
                )
            got = []
            for i in range(10):
                t = q.dequeue(
                    worker_id=f"w{i}", max_running_global=5, max_running_per_tenant=50
                )
                if t is None:
                    break
                got.append(t)
            # max_bg = 5-2 = 3
            self.assertEqual(len(got), 3)
            if bundle.connection is not None:
                bundle.connection.close()

    def test_controlled_borrow_when_interactive_idle(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "borrow.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(
                store=bundle.task_queue_store,
                lease_seconds=60,
                lane_config=LaneCapacityConfig(
                    interactive_reserved=2, background_may_borrow=True
                ),
            )
            for i in range(8):
                q.enqueue(
                    workflow_id=f"b-{i}",
                    task_id=f"t-{i}",
                    execution_key=f"ek-{i}",
                    tenant_id="tA",
                    execution_lane=LANE_BACKGROUND,
                )
            got = []
            for i in range(8):
                t = q.dequeue(
                    worker_id=f"w{i}", max_running_global=5, max_running_per_tenant=50
                )
                if t is None:
                    break
                got.append(t)
            # With borrow and no interactive pending, may use full global=5
            self.assertEqual(len(got), 5)
            if bundle.connection is not None:
                bundle.connection.close()


class FairnessTests(unittest.TestCase):
    def test_tenant_b_not_starved_by_tenant_a_backlog(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "fair.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(
                store=bundle.task_queue_store,
                lease_seconds=60,
                lane_config=LaneCapacityConfig(interactive_reserved=0, fairness_enabled=True),
            )
            for i in range(100):
                q.enqueue(
                    workflow_id=f"a-{i}",
                    task_id=f"ta-{i}",
                    execution_key=f"eka-{i}",
                    tenant_id="tenant-A",
                    execution_lane=LANE_BACKGROUND,
                )
            for i in range(5):
                q.enqueue(
                    workflow_id=f"b-{i}",
                    task_id=f"tb-{i}",
                    execution_key=f"ekb-{i}",
                    tenant_id="tenant-B",
                    execution_lane=LANE_BACKGROUND,
                )
            # Claim one for A so fairness prefers B next among same priority
            first = q.dequeue(
                worker_id="w0", max_running_global=50, max_running_per_tenant=50
            )
            self.assertEqual(first.tenant_id, "tenant-A")
            second = q.dequeue(
                worker_id="w1", max_running_global=50, max_running_per_tenant=50
            )
            self.assertEqual(second.tenant_id, "tenant-B")
            if bundle.connection is not None:
                bundle.connection.close()


class AgingTests(unittest.TestCase):
    def test_old_background_ages_without_lane_change(self):
        old = utc_now() - timedelta(seconds=180)
        rank = effective_priority_rank(
            PRIORITY_LOW,
            created_at=old,
            now=utc_now(),
            aging_seconds_per_step=60.0,
            aging_max_boost=2,
        )
        self.assertEqual(rank, 2)  # low(0)+2 boost, capped below inventing interactive lane
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "age.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            store = bundle.task_queue_store
            q = TaskQueue(
                store=store,
                lease_seconds=60,
                lane_config=LaneCapacityConfig(
                    interactive_reserved=0,
                    aging_seconds_per_step=1.0,
                    aging_max_boost=3,
                    fairness_enabled=False,
                ),
            )
            # Enqueue old low-priority first
            old_task = q.enqueue(
                workflow_id="old",
                task_id="told",
                execution_key="ek-old",
                tenant_id="tA",
                execution_lane=LANE_BACKGROUND,
                priority=PRIORITY_LOW,
            )
            # Force created_at older via save
            from dataclasses import replace

            aged = replace(
                old_task,
                created_at=utc_now() - timedelta(seconds=10),
                available_at=utc_now() - timedelta(seconds=10),
            )
            store.save(aged)
            for i in range(5):
                q.enqueue(
                    workflow_id=f"new-{i}",
                    task_id=f"tn-{i}",
                    execution_key=f"ek-new-{i}",
                    tenant_id="tA",
                    execution_lane=LANE_BACKGROUND,
                    priority=PRIORITY_NORMAL,
                )
            claimed = q.dequeue(
                worker_id="w", max_running_global=10, max_running_per_tenant=50
            )
            self.assertIsNotNone(claimed)
            # Aged low should win over fresh normal once boost >= 1
            self.assertEqual(claimed.execution_lane, LANE_BACKGROUND)
            self.assertEqual(claimed.queue_task_id, old_task.queue_task_id)
            if bundle.connection is not None:
                bundle.connection.close()


class ScheduleStormTests(unittest.TestCase):
    def test_scheduled_storm_preserves_interactive(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "storm.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(
                store=bundle.task_queue_store,
                lease_seconds=60,
                lane_config=LaneCapacityConfig(
                    interactive_reserved=5, background_may_borrow=False
                ),
            )
            for i in range(100):
                q.enqueue(
                    workflow_id=f"s-{i}",
                    task_id=f"ts-{i}",
                    execution_key=f"eks-{i}",
                    tenant_id="sched",
                    execution_lane=LANE_SCHEDULED,
                    metadata={"trigger": "scheduled"},
                )
            q.enqueue(
                workflow_id="ix",
                task_id="tix",
                execution_key="ekix",
                tenant_id="user",
                execution_lane=LANE_INTERACTIVE,
                priority="high",
            )
            claimed = []
            for i in range(20):
                t = q.dequeue(
                    worker_id=f"w{i}", max_running_global=20, max_running_per_tenant=100
                )
                if t is None:
                    break
                claimed.append(t)
            interactive = [c for c in claimed if c.execution_lane == LANE_INTERACTIVE]
            background = [c for c in claimed if c.execution_lane != LANE_INTERACTIVE]
            self.assertGreaterEqual(len(interactive), 1)
            self.assertLessEqual(len(background), 15)
            # Interactive claimed despite 100 scheduled backlog
            self.assertEqual(interactive[0].workflow_id, "ix")
            if bundle.connection is not None:
                bundle.connection.close()


class WorkerLaneFilterTests(unittest.TestCase):
    def test_interactive_only_worker(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "wlane.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(
                store=bundle.task_queue_store,
                lease_seconds=60,
                allowed_lanes=frozenset({LANE_INTERACTIVE}),
                lane_config=LaneCapacityConfig(interactive_reserved=0),
            )
            q.enqueue(
                workflow_id="bg",
                task_id="t",
                execution_key="ek-bg",
                tenant_id="tA",
                execution_lane=LANE_BACKGROUND,
            )
            self.assertIsNone(
                q.dequeue(worker_id="w", max_running_global=10, max_running_per_tenant=10)
            )
            q.enqueue(
                workflow_id="ix",
                task_id="t2",
                execution_key="ek-ix",
                tenant_id="tA",
                execution_lane=LANE_INTERACTIVE,
            )
            got = q.dequeue(
                worker_id="w", max_running_global=10, max_running_per_tenant=10
            )
            self.assertEqual(got.execution_lane, LANE_INTERACTIVE)
            if bundle.connection is not None:
                bundle.connection.close()


class ProviderGovernorTests(unittest.TestCase):
    def test_shared_concurrency_across_workers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "gov.sqlite3")
            limits = GovernorLimits(max_concurrency=2, interactive_reserved=0)
            store = SqliteProviderGovernorStore(path, limits)
            ok = []
            err = []
            barrier = threading.Barrier(4)

            def worker(i):
                barrier.wait()
                gov = ProviderGovernor(store=store, limits=limits)
                try:
                    slot = gov.acquire(
                        provider_id="openai", model_id="m", lane="background", worker_id=f"w{i}"
                    )
                    ok.append(slot)
                    time.sleep(0.05)
                    gov.release(slot)
                except ProviderCapacityUnavailable as exc:
                    err.append(exc.reason)

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(worker, range(4)))
            self.assertLessEqual(len(ok), 2)
            self.assertTrue(err)
            store.close()

    def test_429_throttles_new_acquisitions(self):
        limits = GovernorLimits(max_concurrency=4, failure_threshold=10)
        store = InMemoryProviderGovernorStore(limits)
        gov = ProviderGovernor(store=store, limits=limits)
        gov.record_429("openai", "m", retry_after_seconds=2.0)
        with self.assertRaises(ProviderCapacityUnavailable) as ctx:
            gov.acquire(provider_id="openai", model_id="m")
        self.assertEqual(ctx.exception.reason, "provider_429_throttle")

    def test_breaker_open_half_open_closed(self):
        limits = GovernorLimits(
            max_concurrency=4,
            failure_threshold=2,
            cooldown_seconds=0.2,
            half_open_probe_limit=1,
        )
        store = InMemoryProviderGovernorStore(limits)
        gov = ProviderGovernor(store=store, limits=limits)
        gov.record_failure("openai", "m", error_code="TimeoutError")
        gov.record_failure("openai", "m", error_code="TimeoutError")
        self.assertEqual(gov.breaker_state("openai", "m"), STATE_OPEN)
        time.sleep(0.25)
        self.assertEqual(gov.breaker_state("openai", "m"), STATE_HALF_OPEN)
        slot = gov.acquire(provider_id="openai", model_id="m", worker_id="probe")
        gov.release(slot)
        gov.record_success("openai", "m")
        self.assertEqual(gov.breaker_state("openai", "m"), STATE_CLOSED)

    def test_non_qualifying_error_does_not_open_breaker(self):
        limits = GovernorLimits(failure_threshold=1)
        store = InMemoryProviderGovernorStore(limits)
        gov = ProviderGovernor(store=store, limits=limits)
        gov.record_failure("openai", "m", error_code="validation_error")
        self.assertEqual(gov.breaker_state("openai", "m"), STATE_CLOSED)

    def test_provider_interactive_reserved(self):
        limits = GovernorLimits(
            max_concurrency=3, interactive_reserved=1, background_may_borrow=False
        )
        store = InMemoryProviderGovernorStore(limits)
        gov = ProviderGovernor(store=store, limits=limits)
        s1 = gov.acquire(provider_id="p", model_id="m", lane="background")
        s2 = gov.acquire(provider_id="p", model_id="m", lane="background")
        with self.assertRaises(ProviderCapacityUnavailable):
            gov.acquire(provider_id="p", model_id="m", lane="background")
        s3 = gov.acquire(provider_id="p", model_id="m", lane="interactive")
        gov.release(s1)
        gov.release(s2)
        gov.release(s3)


class RouterCapacityFallbackTests(unittest.TestCase):
    def test_capacity_unavailable_uses_fallback_path(self):
        class _Gov:
            def breaker_state(self, provider_id, model_id=""):
                return STATE_OPEN if provider_id == "openai" else STATE_CLOSED

            def is_available(self, provider_id, model_id="", *, lane="background"):
                return provider_id != "openai"

        class _Reg:
            def active_provider_ids(self):
                return ("openai", "anthropic")

            def model(self, provider_id):
                return "m"

            def profile(self, provider_id):
                return None

        router = ModelRouter(_Reg(), capacity_governor=_Gov())
        kept = router._filter_by_capacity(("openai", "anthropic"))
        self.assertEqual(kept, ("anthropic",))
        blocked = router._capacity_blocked_set(("openai", "anthropic"))
        self.assertIn("openai", blocked)
        self.assertEqual(REJECT_CAPACITY_UNAVAILABLE, "capacity_unavailable")


class LoadShapeTests(unittest.TestCase):
    def test_500_background_50_interactive_invariants(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "load.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path, MAX_RUNNING_GLOBAL="20", INTERACTIVE_RESERVED="5"),
                durable=True,
                run_recovery_scan=False,
            )
            q = TaskQueue(
                store=bundle.task_queue_store,
                lease_seconds=60,
                lane_config=LaneCapacityConfig(
                    interactive_reserved=5, background_may_borrow=False, fairness_enabled=True
                ),
            )
            for i in range(500):
                q.enqueue(
                    workflow_id=f"bg-{i}",
                    task_id=f"tbg-{i}",
                    execution_key=f"ekbg-{i}",
                    tenant_id=f"tenant-{i % 10}",
                    execution_lane=LANE_BACKGROUND,
                    priority=PRIORITY_LOW,
                )
            for i in range(50):
                q.enqueue(
                    workflow_id=f"ix-{i}",
                    task_id=f"tix-{i}",
                    execution_key=f"ekix-{i}",
                    tenant_id=f"user-{i % 5}",
                    execution_lane=LANE_INTERACTIVE,
                    priority="high",
                )
            claimed = []
            busy_before = getattr(bundle.task_queue_store, "sqlite_busy_count", 0)
            for i in range(25):
                t = q.dequeue(
                    worker_id=f"w{i}",
                    max_running_global=20,
                    max_running_per_tenant=10,
                )
                if t is None:
                    break
                claimed.append(t)
            self.assertEqual(len(claimed), 20)
            # Unique ownership
            self.assertEqual(len({c.queue_task_id for c in claimed}), 20)
            interactive = [c for c in claimed if c.execution_lane == LANE_INTERACTIVE]
            background = [c for c in claimed if c.execution_lane != LANE_INTERACTIVE]
            self.assertGreaterEqual(len(interactive), 1)
            self.assertLessEqual(len(background), 15)  # global 20 - reserved 5
            busy_after = getattr(bundle.task_queue_store, "sqlite_busy_count", 0)
            self.assertGreaterEqual(busy_after, busy_before)
            if bundle.connection is not None:
                bundle.connection.close()


if __name__ == "__main__":
    unittest.main()
