"""Production Runtime / Scale — Mega-Block 3.13-3.26: Production Workload
Execution & Worker Isolation.

Most of the queue/lane isolation, pool-aware admission, tenant-fairness (SQL
``claim_next`` ROW_NUMBER partitioning), tenant quotas (``AdmissionLimits``),
heavy-job routing (excel/crawler/media -> batch/background), and the
autoscaling snapshot contract already exist and are covered by
``tests/test_production_runtime_scale.py``, ``tests/test_acquisition_scale_platform.py``,
``tests/test_data_intelligence_block7_platform.py`` and
``tests/test_production_runtime_scale_3_12_interactive_workload.py``. This
file targets ONLY the genuinely new/hardened surface added for this
mega-block:

- 3.15 pool-specific concurrency: previously configured
  (``WORKER_POOL_MAX_CONCURRENCY``/``WORKER_MAX_CONCURRENCY``) but never
  actually enforced -- the production worker loop always ran exactly one
  task at a time regardless of configuration. Fixed in ``workflow/service.py``.
- 3.24 autoscaling contract enrichment: ``CapacitySnapshot.pool_concurrency``.
- 3.25 graceful drain hardened for >1 concurrent in-flight slots.
- 3.26 crash/restart recovery hardening proven against a durable BATCH lease
  crash using the existing (unmodified) lease-reclaim contract.
- Regression guard: pool/lane isolation still holds through the newly-wired
  ``build_workflow_runtime`` concurrency configuration path.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from config.runtime_config import validate_runtime_config
from runtime.capacity_snapshot import build_capacity_snapshot
from side_effects.persistence import build_side_effect_persistence
from task_queue.lanes import LANE_BACKGROUND, LANE_BULK, LANE_INTERACTIVE
from task_queue.models import STATUS_COMPLETED, STATUS_RETRY_WAIT, STATUS_RUNNING
from task_queue.pools import POOL_BATCH
from task_queue.queue import TaskQueue
from task_queue.store import InMemoryTaskQueueStore
from workflow.service import build_workflow_runtime


class PoolConcurrencyWiringTests(unittest.TestCase):
    """3.15 — configuration is typed/validated and actually reaches the worker."""

    def test_build_workflow_runtime_uses_configured_pool_concurrency(self):
        wr = build_workflow_runtime(
            env={
                "WORKER_POOL": "batch",
                "WORKER_POOL_MAX_CONCURRENCY": "4",
                "WORKER_LANES": "all",
            }
        )
        self.assertEqual(wr.worker.config.pool_name, POOL_BATCH)
        self.assertEqual(wr.worker.config.max_concurrency, 4)

    def test_default_pool_concurrency_is_backward_compatible_one(self):
        # No env set -> unchanged default behaviour (single sequential slot).
        wr = build_workflow_runtime(env={})
        self.assertEqual(wr.worker.config.max_concurrency, 1)

    def test_runtime_config_exposes_typed_pool_fields_with_safe_default(self):
        cfg = validate_runtime_config({}, raise_on_error=False)
        self.assertGreaterEqual(cfg.worker_pool_max_concurrency, 1)
        self.assertTrue(cfg.worker_pool_name)

    def test_runtime_config_invalid_pool_concurrency_fails_safe(self):
        # Negative/garbage input must never disable the worker or raise --
        # it degrades to the safe minimum of 1, matching PoolConfig.from_env.
        cfg = validate_runtime_config(
            {"WORKER_POOL_MAX_CONCURRENCY": "-5"}, raise_on_error=False
        )
        self.assertEqual(cfg.worker_pool_max_concurrency, 1)
        self.assertEqual(cfg.errors, ())


class ConcurrencySnapshotContractTests(unittest.TestCase):
    """3.24 — stable, importable pool-concurrency signal."""

    def test_concurrency_snapshot_shape_before_start(self):
        wr = build_workflow_runtime(
            env={"WORKER_POOL": "interactive", "WORKER_POOL_MAX_CONCURRENCY": "5"}
        )
        snap = wr.concurrency_snapshot()
        self.assertEqual(snap["pool_name"], "interactive")
        self.assertEqual(snap["max_concurrency"], 5)
        self.assertEqual(snap["active"], 0)
        self.assertEqual(snap["available"], 5)
        self.assertFalse(snap["draining"])


class CapacitySnapshotPoolConcurrencyTests(unittest.TestCase):
    """3.24 — autoscaling contract can be enriched with pool concurrency."""

    def test_pool_concurrency_signal_included_when_supplied(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        snap = build_capacity_snapshot(
            q,
            pool_concurrency={
                "batch": {
                    "pool_name": "batch",
                    "max_concurrency": 4,
                    "active": 1,
                    "available": 3,
                    "draining": False,
                }
            },
        )
        d = snap.as_dict()
        self.assertIn("pool_concurrency", d)
        self.assertEqual(d["pool_concurrency"]["batch"]["max_concurrency"], 4)

    def test_pool_concurrency_defaults_empty_and_backward_compatible(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        snap = build_capacity_snapshot(q)
        self.assertEqual(snap.as_dict()["pool_concurrency"], {})


class PoolConcurrencyEnforcementTests(unittest.IsolatedAsyncioTestCase):
    """3.15 — deterministic, event-based (no sleep-timing) concurrency proof."""

    async def test_pool_runs_up_to_configured_limit_and_never_more(self):
        wr = build_workflow_runtime(
            env={"WORKER_POOL_MAX_CONCURRENCY": "3", "WORKER_LANES": "all"}
        )
        limit = 3
        state = {"active": 0, "max_active": 0, "violation": False, "completed": 0}
        release = asyncio.Event()
        reached_limit = asyncio.Event()
        lock = asyncio.Lock()

        async def handler(ctx):
            async with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                if state["active"] > limit:
                    state["violation"] = True
                if state["active"] >= limit:
                    reached_limit.set()
            await release.wait()
            async with lock:
                state["active"] -= 1
                state["completed"] += 1
            return {"ok": True}

        total_tasks = limit + 2
        for i in range(total_tasks):
            key = f"ek-conc-{i}"
            wr.registry.register(key, handler)
            wr.queue.enqueue(
                workflow_id=f"wf-conc-{i}",
                task_id=f"t-{i}",
                execution_key=key,
                tenant_id="tenant-conc",
                execution_lane=LANE_BULK,
            )

        await wr.start_background(poll_interval=0.02)
        try:
            try:
                await asyncio.wait_for(reached_limit.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self.fail(
                    "pool never reached its configured concurrency limit -- "
                    "3.15 concurrency is not actually enforced/parallelized"
                )
            self.assertLessEqual(state["max_active"], limit)
            self.assertGreaterEqual(
                state["max_active"], 2, "expected real parallel execution, not serialized"
            )
            release.set()
            for _ in range(250):
                if state["completed"] >= total_tasks:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(state["completed"], total_tasks)
            self.assertFalse(state["violation"])
        finally:
            await wr.stop_background()


class GracefulDrainConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """3.25 — drain stops new claims but bounded-waits for in-flight slots."""

    async def test_drain_blocks_new_claims_and_lets_inflight_finish(self):
        wr = build_workflow_runtime(
            env={"WORKER_POOL_MAX_CONCURRENCY": "2", "WORKER_LANES": "all"}
        )
        release = asyncio.Event()
        started = asyncio.Event()
        state = {"active": 0, "completed": []}

        async def handler(ctx):
            state["active"] += 1
            if state["active"] >= 2:
                started.set()
            await release.wait()
            state["active"] -= 1
            state["completed"].append(ctx.queue_task_id)
            return {"ok": True}

        for i in range(4):
            key = f"ek-drain-{i}"
            wr.registry.register(key, handler)
            wr.queue.enqueue(
                workflow_id=f"wf-drain-{i}",
                task_id=f"t-{i}",
                execution_key=key,
                tenant_id="tenant-drain",
                execution_lane=LANE_BACKGROUND,
            )

        await wr.start_background(poll_interval=0.02)
        await asyncio.wait_for(started.wait(), timeout=3.0)

        # Begin drain: 2 slots are occupied (blocked on `release`); 2 tasks
        # remain queued. No NEW claims must happen while draining.
        wr.stop_new_claims()
        self.assertTrue(wr.concurrency_snapshot()["draining"])
        await asyncio.sleep(0.1)
        remaining_pending = sum(
            1
            for t in wr.queue.store.list_all()
            if t.status in {"queued", "retry_wait"}
        )
        self.assertEqual(remaining_pending, 2)

        release.set()
        await wr.stop_background(drain_timeout_seconds=2.0)
        # Both already-in-flight slots completed cleanly; drain did not
        # silently drop or double-complete them.
        self.assertEqual(len(state["completed"]), 2)
        self.assertEqual(len(set(state["completed"])), 2)


class MultiPoolIsolationWithWiredConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """Regression guard: 3.14 lane isolation still holds after the 3.15 wiring
    change (a batch-pool worker configured via env must never claim interactive
    work, even though it now also carries a concurrency>1 configuration)."""

    async def test_batch_pool_worker_never_claims_interactive_lane(self):
        store = InMemoryTaskQueueStore()
        wr_batch = build_workflow_runtime(
            task_queue_store=store,
            env={
                "WORKER_POOL": "batch",
                "WORKER_LANES": "bulk,scheduled",
                "WORKER_POOL_MAX_CONCURRENCY": "2",
            },
        )
        self.assertEqual(wr_batch.worker.config.pool_name, POOL_BATCH)
        self.assertEqual(wr_batch.worker.config.max_concurrency, 2)

        wr_batch.queue.enqueue(
            workflow_id="wf-ix",
            task_id="t",
            execution_key="ek-ix-iso",
            tenant_id="t1",
            execution_lane=LANE_INTERACTIVE,
            priority="critical",
        )
        claimed = wr_batch.queue.dequeue(worker_id="probe")
        self.assertIsNone(claimed)

        wr_batch.queue.enqueue(
            workflow_id="wf-batch",
            task_id="t2",
            execution_key="ek-batch-iso",
            tenant_id="t1",
            execution_lane=LANE_BULK,
        )
        claimed_batch = wr_batch.queue.dequeue(worker_id="probe")
        self.assertIsNotNone(claimed_batch)
        self.assertEqual(claimed_batch.execution_lane, LANE_BULK)


class CrashRestartRecoveryHardeningTests(unittest.TestCase):
    """3.26 — hardens the existing lease-reclaim contract for the new
    multi-pool runtime: a durable BATCH job crashes mid-execution; after
    "restart" (a brand-new TaskQueue bound to the same durable store), the
    job is recoverable with workload/lane/tenant metadata intact, and
    completes exactly once (no silent loss, no duplicate completion)."""

    def test_batch_job_lease_crash_recovered_with_metadata_intact(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "crash.sqlite3")
            bundle = build_side_effect_persistence(
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                },
                durable=True,
                run_recovery_scan=False,
            )
            q1 = TaskQueue(store=bundle.task_queue_store, lease_seconds=1)
            q1.enqueue(
                workflow_id="wf-crash",
                task_id="t1",
                execution_key="ek-crash-1",
                tenant_id="tenant-crash",
                execution_lane=LANE_BULK,
                priority="low",
                metadata={
                    "workload_class": "batch",
                    "trusted_job_type": "crawler",
                },
            )
            claimed = q1.dequeue(worker_id="pool-batch-slot-0")
            self.assertIsNotNone(claimed)
            running = q1.start(
                claimed.queue_task_id, claimed.lease_id, worker_id="pool-batch-slot-0"
            )
            self.assertEqual(running.status, STATUS_RUNNING)

            # Simulated crash: process holding the lease is gone. Never
            # heartbeats/acks again. Lease expires.
            past = running.lease_expires_at + timedelta(seconds=1)

            # "Restart": a brand-new TaskQueue instance over the SAME
            # durable store, exactly as a fresh worker process would build.
            q2 = TaskQueue(store=bundle.task_queue_store, lease_seconds=60)
            reclaimed_ids = q2.recover_stuck_running(now=past, force=True)
            self.assertIn(running.queue_task_id, reclaimed_ids)

            after_reclaim = q2.get(running.queue_task_id)
            self.assertEqual(after_reclaim.status, STATUS_RETRY_WAIT)
            self.assertEqual(after_reclaim.execution_lane, LANE_BULK)
            self.assertEqual(after_reclaim.tenant_id, "tenant-crash")
            self.assertEqual(after_reclaim.metadata.get("workload_class"), "batch")
            self.assertEqual(after_reclaim.metadata.get("trusted_job_type"), "crawler")

            restored = q2.dequeue(worker_id="pool-batch-slot-1", now=past)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.queue_task_id, running.queue_task_id)
            self.assertEqual(restored.execution_lane, LANE_BULK)
            self.assertEqual(restored.tenant_id, "tenant-crash")
            self.assertEqual(restored.metadata.get("workload_class"), "batch")

            q2.start(
                restored.queue_task_id,
                restored.lease_id,
                worker_id="pool-batch-slot-1",
                now=past,
            )
            done = q2.ack(
                restored.queue_task_id,
                restored.lease_id,
                worker_id="pool-batch-slot-1",
                now=past,
            )
            self.assertEqual(done.status, STATUS_COMPLETED)

            # No accidental duplicate successful completion: the original
            # crashed lease/worker can no longer ack the same task.
            from task_queue.errors import QueueLeaseError

            with self.assertRaises(QueueLeaseError):
                q2.ack(claimed.queue_task_id, claimed.lease_id, worker_id="pool-batch-slot-0")

            if bundle.connection is not None:
                bundle.connection.close()


if __name__ == "__main__":
    unittest.main()
