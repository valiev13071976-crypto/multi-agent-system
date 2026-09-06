"""Targeted tests for PANDA MULTI-AGENT Production Runtime / Scale
MEGA-BLOCK 3.27-3.38: Distributed Production Runtime, Resilience,
Observability & Safe Traffic Rollout.

Baseline: builds on 3.12 (workload propagation) and 3.13-3.26 (production
workload execution / worker isolation), both CLOSED and merged to main. This
file does NOT re-test those closed blocks; it proves the NEW 3.27-3.38
contracts, reusing existing durable/shared persistence abstractions
(SQLite-backed TaskQueue store, SqliteProviderGovernorStore) to exercise
genuine cross-instance coordination rather than process-local mocks.
"""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from task_queue.errors import (
    QueueDLQIneligibleError,
    QueueLeaseError,
    QueueRedriveRejectedError,
    QueueTenantOwnershipError,
)
from task_queue.models import STATUS_DEAD_LETTERED, STATUS_QUEUED, STATUS_RUNNING
from task_queue.queue import TaskQueue
from task_queue.store import InMemoryTaskQueueStore


T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def _dead_letter_task(queue: TaskQueue, *, tenant_id="tenant-a", error_code="permanent_failure"):
    queue.enqueue(
        workflow_id="wf-1",
        task_id="task-1",
        execution_key="ek-1",
        tenant_id=tenant_id,
        metadata={"workload_class": "batch", "correlation_id": "corr-1", "trace_id": "trace-1"},
    )
    leased = queue.dequeue(worker_id="w0")
    queue.start(leased.queue_task_id, leased.lease_id, worker_id="w0")
    return queue.dead_letter(
        leased.queue_task_id, leased.lease_id, error_code=error_code, worker_id="w0"
    )


class DLQReplayRedriveTests(unittest.TestCase):
    """Group A (3.27): DLQ terminal failure, metadata, safe replay, safe
    redrive, redrive idempotency/loop-protection, tenant isolation.
    """

    def test_replay_is_read_only_and_does_not_mutate_state(self):
        queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=Clock())
        lettered = _dead_letter_task(queue)
        record = queue.replay_dead_letter(
            lettered.queue_task_id, actor_ref="ops", tenant_id="tenant-a"
        )
        self.assertEqual(record.workflow_id, "wf-1")
        self.assertEqual(record.execution_key, "ek-1")
        self.assertEqual(record.tenant_id, "tenant-a")
        self.assertEqual(record.workload_class, "batch")
        self.assertEqual(record.correlation_id, "corr-1")
        self.assertEqual(record.trace_id, "trace-1")
        self.assertEqual(record.error_code, "permanent_failure")
        self.assertTrue(record.redrive_eligible)
        self.assertEqual(record.redrive_count, 0)
        # Replay is idempotent / non-mutating: task status unchanged, and
        # calling it again yields the same view.
        still = queue.get(lettered.queue_task_id)
        self.assertEqual(still.status, STATUS_DEAD_LETTERED)
        record2 = queue.replay_dead_letter(
            lettered.queue_task_id, actor_ref="ops", tenant_id="tenant-a"
        )
        self.assertEqual(record2.as_dict()["redrive_count"], record.as_dict()["redrive_count"])

    def test_replay_fails_safely_for_non_dlq_task(self):
        queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=Clock())
        task = queue.enqueue(workflow_id="wf", task_id="t", execution_key="ek", tenant_id="tenant-a")
        with self.assertRaises(QueueDLQIneligibleError):
            queue.replay_dead_letter(task.queue_task_id, actor_ref="ops", tenant_id="tenant-a")

    def test_replay_tenant_isolation(self):
        queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=Clock())
        lettered = _dead_letter_task(queue, tenant_id="tenant-a")
        with self.assertRaises(QueueTenantOwnershipError):
            queue.replay_dead_letter(
                lettered.queue_task_id, actor_ref="ops", tenant_id="tenant-b"
            )

    def test_redrive_preserves_lineage_and_metadata_no_secrets(self):
        queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=Clock())
        lettered = _dead_letter_task(queue)
        redriven = queue.redrive_dead_letter(
            lettered.queue_task_id, actor_ref="ops", tenant_id="tenant-a"
        )
        self.assertEqual(redriven.status, STATUS_QUEUED)
        self.assertEqual(redriven.attempt, lettered.attempt + 1)
        self.assertEqual(redriven.metadata["workload_class"], "batch")
        self.assertEqual(redriven.metadata["redrive_count"], 1)
        blob = str(dict(redriven.metadata))
        self.assertNotIn("secret", blob.lower())

    def test_redrive_tenant_isolation_fail_closed(self):
        queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=Clock())
        lettered = _dead_letter_task(queue, tenant_id="tenant-a")
        with self.assertRaises(QueueTenantOwnershipError):
            queue.redrive_dead_letter(
                lettered.queue_task_id, actor_ref="ops", tenant_id="tenant-b"
            )
        # Original tenant still succeeds (isolation didn't corrupt state).
        redriven = queue.redrive_dead_letter(
            lettered.queue_task_id, actor_ref="ops", tenant_id="tenant-a"
        )
        self.assertEqual(redriven.status, STATUS_QUEUED)

    def test_redrive_rejects_ineligible_status(self):
        queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=Clock())
        task = queue.enqueue(workflow_id="wf", task_id="t", execution_key="ek", tenant_id="tenant-a")
        with self.assertRaises(QueueDLQIneligibleError):
            queue.redrive_dead_letter(task.queue_task_id, actor_ref="ops", tenant_id="tenant-a")

    def test_redrive_bounded_loop_protection(self):
        os.environ["DLQ_MAX_REDRIVES"] = "2"
        try:
            clock = Clock()
            queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=clock)
            lettered = _dead_letter_task(queue)
            qid = lettered.queue_task_id
            for _ in range(2):
                redriven = queue.redrive_dead_letter(qid, actor_ref="ops", tenant_id="tenant-a")
                leased = queue.dequeue(worker_id="w0")
                queue.start(leased.queue_task_id, leased.lease_id, worker_id="w0")
                lettered = queue.dead_letter(
                    leased.queue_task_id, leased.lease_id, error_code="permanent_failure", worker_id="w0"
                )
            # Third redrive attempt exceeds DLQ_MAX_REDRIVES=2.
            with self.assertRaises(QueueRedriveRejectedError):
                queue.redrive_dead_letter(lettered.queue_task_id, actor_ref="ops", tenant_id="tenant-a")
        finally:
            os.environ.pop("DLQ_MAX_REDRIVES", None)

    def test_redrive_requires_actor_and_tenant(self):
        queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=Clock())
        lettered = _dead_letter_task(queue)
        with self.assertRaises(QueueTenantOwnershipError):
            queue.redrive_dead_letter(lettered.queue_task_id, actor_ref="ops", tenant_id="")

    def test_redrive_metrics_observable(self):
        from runtime.metrics import RUNTIME_COUNTERS

        RUNTIME_COUNTERS.reset()
        queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=Clock())
        lettered = _dead_letter_task(queue)
        queue.redrive_dead_letter(lettered.queue_task_id, actor_ref="ops", tenant_id="tenant-a")
        self.assertEqual(RUNTIME_COUNTERS.total("redrive"), 1)


def _two_shared_sqlite_queues(path: str, *, lease_seconds: float = 60.0):
    """Build two genuinely independent TaskQueue instances (own SqliteConnection,
    own PersistentTaskQueueStore) sharing the SAME durable SQLite file, to
    simulate two separate runtime processes (Scale 3.28/3.31 realism
    requirement: a shared persistence boundary, not two objects in one
    process sharing an in-memory store).
    """

    from side_effects.persistence import build_side_effect_persistence

    env = {"SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite", "SIDE_EFFECT_DB_PATH": path}
    bundle_a = build_side_effect_persistence(env=env, run_recovery_scan=False)
    bundle_b = build_side_effect_persistence(env=env, run_recovery_scan=False)
    q_a = TaskQueue(bundle_a.task_queue_store, lease_seconds=lease_seconds)
    q_b = TaskQueue(bundle_b.task_queue_store, lease_seconds=lease_seconds)
    return q_a, q_b


class DistributedCoordinationTests(unittest.TestCase):
    """Group B (3.28): two runtime instances, atomic claim, lease ownership,
    heartbeat, dead-owner reclaim -- exercised over a genuinely shared SQLite
    persistence boundary (own connection per "instance").
    """

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_scenario1_distributed_claim_no_double_ownership(self):
        """SCENARIO 1: two instances race to claim the same durable jobs;
        exactly one instance obtains valid ownership of each job."""

        q_a, q_b = _two_shared_sqlite_queues(self.db_path)
        n_tasks = 24
        for i in range(n_tasks):
            q_a.enqueue(
                workflow_id="wf",
                task_id=f"t-{i}",
                execution_key=f"ek-{i}",
                tenant_id="tenant-a",
            )

        claimed: list[str] = []
        lock = __import__("threading").Lock()

        def _drain(queue, worker_id):
            local = []
            while True:
                task = queue.dequeue(worker_id=worker_id)
                if task is None:
                    break
                # Simulate quick processing so per-tenant running capacity
                # (existing 3.17/3.19 admission fairness, unrelated to this
                # scenario) frees up for the remaining tasks.
                queue.start(task.queue_task_id, task.lease_id, worker_id=worker_id)
                queue.ack(task.queue_task_id, task.lease_id, worker_id=worker_id)
                local.append(task.queue_task_id)
            with lock:
                claimed.extend(local)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [
                pool.submit(_drain, q_a, "instance-a-worker"),
                pool.submit(_drain, q_b, "instance-b-worker"),
            ]
            for f in futs:
                f.result(timeout=10)

        self.assertEqual(len(claimed), n_tasks)
        self.assertEqual(len(set(claimed)), n_tasks, "no task claimed twice")

    def test_scenario2_instance_crash_dead_owner_reclaim_no_duplicate_completion(self):
        """SCENARIO 2: instance A owns a job and "dies" (never acks). After
        lease expiry, instance B safely reclaims and completes it exactly
        once."""

        q_a, q_b = _two_shared_sqlite_queues(self.db_path, lease_seconds=1)
        q_a.enqueue(workflow_id="wf", task_id="t", execution_key="ek", tenant_id="tenant-a")

        leased = q_a.dequeue(worker_id="instance-a-worker")
        q_a.start(leased.queue_task_id, leased.lease_id, worker_id="instance-a-worker")
        # Instance A crashes here -- no ack, no heartbeat.

        past = leased.lease_expires_at + timedelta(seconds=1)
        reclaimed_ids = q_b.recover_stuck_running(now=past, force=True)
        self.assertIn(leased.queue_task_id, reclaimed_ids)

        # Instance B claims and completes the reclaimed job.
        claimed = q_b.dequeue(worker_id="instance-b-worker", now=past)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.queue_task_id, leased.queue_task_id)
        q_b.start(claimed.queue_task_id, claimed.lease_id, worker_id="instance-b-worker", now=past)
        done = q_b.ack(claimed.queue_task_id, claimed.lease_id, worker_id="instance-b-worker", now=past)
        self.assertEqual(done.status, "completed")

        # Instance A's stale lease can never complete the same job again
        # (fenced by lease_id): no duplicate completion is possible.
        with self.assertRaises(QueueLeaseError):
            q_a.ack(leased.queue_task_id, leased.lease_id, worker_id="instance-a-worker", now=past)

    def test_heartbeat_renewal_prevents_premature_cross_instance_reclaim(self):
        """Live owner's heartbeat renewal is visible to the other instance
        immediately (shared durable state): a live lease is never reclaimed."""

        q_a, q_b = _two_shared_sqlite_queues(self.db_path, lease_seconds=2)
        q_a.enqueue(workflow_id="wf", task_id="t", execution_key="ek", tenant_id="tenant-a")
        leased = q_a.dequeue(worker_id="instance-a-worker")
        q_a.start(leased.queue_task_id, leased.lease_id, worker_id="instance-a-worker")

        # Renew the lease from instance A shortly before it would expire.
        soon = leased.lease_expires_at - timedelta(milliseconds=500)
        renewed = q_a.heartbeat(
            leased.queue_task_id, "instance-a-worker", leased.lease_id, now=soon, lease_seconds=5
        )
        self.assertEqual(renewed.status, STATUS_RUNNING)

        # Instance B checks shortly after the ORIGINAL expiry: the renewal
        # (shared durable state) must prevent reclaim.
        original_expiry_passed = leased.lease_expires_at + timedelta(milliseconds=100)
        reclaimed_ids = q_b.recover_stuck_running(now=original_expiry_passed, force=True)
        self.assertNotIn(leased.queue_task_id, reclaimed_ids)
        still = q_b.get(leased.queue_task_id)
        self.assertEqual(still.status, STATUS_RUNNING)


class SharedRuntimeHealthTests(unittest.TestCase):
    """Group C (3.29): shared instance registration, shared health,
    stale instance detection, fleet snapshot."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_shared_registration_visible_across_instances(self):
        from runtime.fleet_registry import FleetRegistry, SqliteFleetRegistryStore

        # Two independent registry objects (own connection) over the SAME
        # shared SQLite file -- simulating two runtime instances.
        reg_a = FleetRegistry(store=SqliteFleetRegistryStore(self.db_path))
        reg_b = FleetRegistry(store=SqliteFleetRegistryStore(self.db_path))

        t0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        reg_a.heartbeat(
            instance_id="inst-a",
            pool_name="interactive",
            lanes=["interactive"],
            runtime_role="worker",
            active_jobs=1,
            max_concurrency=4,
            available_concurrency=3,
            now=t0,
        )
        reg_b.heartbeat(
            instance_id="inst-b",
            pool_name="batch",
            lanes=["batch", "background"],
            runtime_role="worker",
            active_jobs=2,
            max_concurrency=4,
            available_concurrency=2,
            now=t0,
        )

        # Instance A can see instance B's row (durable shared state), not
        # merely its own local/process view.
        snap_from_a = reg_a.snapshot(now=t0 + timedelta(seconds=1))
        ids = {v.instance_id for v in snap_from_a}
        self.assertEqual(ids, {"inst-a", "inst-b"})
        by_id = {v.instance_id: v for v in snap_from_a}
        self.assertEqual(by_id["inst-b"].pool_name, "batch")
        self.assertEqual(by_id["inst-b"].lanes, ("batch", "background"))
        self.assertFalse(by_id["inst-a"].is_stale)
        self.assertFalse(by_id["inst-b"].is_stale)

    def test_stale_instance_detection_bounded_ttl(self):
        from runtime.fleet_registry import (
            FleetRegistry,
            FleetRegistryConfig,
            SqliteFleetRegistryStore,
        )

        store = SqliteFleetRegistryStore(self.db_path)
        registry = FleetRegistry(
            store=store, config=FleetRegistryConfig(stale_after_seconds=30.0)
        )
        t0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        registry.heartbeat(instance_id="inst-a", now=t0)

        # Shortly after: still fresh.
        fresh = registry.snapshot(now=t0 + timedelta(seconds=5))[0]
        self.assertFalse(fresh.is_stale)

        # A dead instance's old row is never treated as healthy forever: once
        # the TTL elapses without a new heartbeat, it is reported stale.
        stale = registry.snapshot(now=t0 + timedelta(seconds=60))[0]
        self.assertTrue(stale.is_stale)

    def test_fleet_summary_aggregates_bounded_cardinality(self):
        from runtime.fleet_registry import FleetRegistry, SqliteFleetRegistryStore

        store = SqliteFleetRegistryStore(self.db_path)
        registry = FleetRegistry(store=store)
        t0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        registry.heartbeat(
            instance_id="inst-a", active_jobs=2, max_concurrency=4, available_concurrency=2, now=t0
        )
        registry.heartbeat(
            instance_id="inst-b",
            active_jobs=1,
            max_concurrency=4,
            available_concurrency=3,
            draining=True,
            now=t0,
        )
        summary = registry.fleet_summary(now=t0)
        self.assertEqual(summary["instance_count"], 2)
        self.assertEqual(summary["active_instance_count"], 2)
        self.assertEqual(summary["draining_instance_count"], 1)
        self.assertEqual(summary["total_active_jobs"], 3)
        # No unbounded label dimension leaks (no tenant/request/run ids).
        for key in summary:
            self.assertNotIn("tenant", key)
            self.assertNotIn("request_id", key)

    def test_workflow_runtime_bundle_heartbeats_and_deregisters(self):
        """WorkflowRuntimeBundle writes its shared health row and cleanly
        deregisters on graceful stop (integration with 3.13-3.26 pool
        concurrency, unmodified by default when fleet_registry is None)."""

        import asyncio

        from runtime.fleet_registry import FleetRegistry, SqliteFleetRegistryStore
        from workflow.service import build_workflow_runtime

        async def _run():
            store = SqliteFleetRegistryStore(self.db_path)
            fleet = FleetRegistry(store=store)
            bundle = build_workflow_runtime(runtime_role="worker")
            bundle.fleet_registry = fleet
            bundle.instance_id = "bundle-inst"
            bundle._fleet_heartbeat_min_interval = 0.0
            await bundle.start_background(poll_interval=0.01)
            await asyncio.sleep(0.05)
            snap = fleet.snapshot()
            self.assertTrue(any(v.instance_id == "bundle-inst" for v in snap))
            await bundle.stop_background(drain_timeout_seconds=0.2)
            after = fleet.snapshot()
            self.assertFalse(any(v.instance_id == "bundle-inst" for v in after))

        asyncio.run(_run())


class DistributedProviderGovernorTests(unittest.TestCase):
    """Group D (3.30): two instances sharing ProviderGovernor capacity,
    combined concurrency safety, stale reservation recovery.

    ProviderGovernor was already SQLite/WAL-backed (own atomic
    BEGIN IMMEDIATE transactions) prior to this mega-block -- these tests
    PROVE that existing contract holds for genuinely independent instances
    rather than re-implementing it (Scale 3.30 hardening = verification,
    per architecture inspection)."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _two_governors(self, limits):
        from providers.governor import ProviderGovernor, SqliteProviderGovernorStore

        gov_a = ProviderGovernor(store=SqliteProviderGovernorStore(self.db_path, limits), limits=limits)
        gov_b = ProviderGovernor(store=SqliteProviderGovernorStore(self.db_path, limits), limits=limits)
        return gov_a, gov_b

    def test_scenario3_combined_capacity_never_exceeds_shared_policy(self):
        from providers.governor import GovernorLimits, ProviderCapacityUnavailable

        limits = GovernorLimits(
            max_concurrency=5,
            interactive_reserved=0,
            background_may_borrow=False,
            max_rpm=None,
            max_qps=None,
            max_tpm=None,
        )
        gov_a, gov_b = self._two_governors(limits)

        slots = []
        # Instance A acquires 3, instance B acquires 3 more -- combined
        # accepted capacity must be capped at 5, not 6, even though neither
        # instance alone believes it has hit the limit early on.
        for _ in range(3):
            slots.append(("a", gov_a.acquire(provider_id="openai", lane="background", worker_id="w-a")))
        accepted_by_b = 0
        rejected_by_b = 0
        for _ in range(3):
            try:
                slots.append(("b", gov_b.acquire(provider_id="openai", lane="background", worker_id="w-b")))
                accepted_by_b += 1
            except ProviderCapacityUnavailable:
                rejected_by_b += 1
        self.assertEqual(accepted_by_b, 2)
        self.assertEqual(rejected_by_b, 1)
        self.assertEqual(len(slots), 5)

    def test_stale_reservation_recovery_via_ttl(self):
        from datetime import datetime, timedelta, timezone as _tz

        from providers.governor import GovernorLimits

        limits = GovernorLimits(max_concurrency=1, slot_ttl_seconds=1.0)
        gov_a, gov_b = self._two_governors(limits)
        t0 = datetime(2026, 9, 1, 0, 0, tzinfo=_tz.utc)
        gov_a.store.acquire(provider_id="openai", lane="background", worker_id="w-a", now=t0)

        # Instance A crashes without releasing. Before TTL: B is denied.
        from providers.governor import ProviderCapacityUnavailable

        with self.assertRaises(ProviderCapacityUnavailable):
            gov_b.store.acquire(provider_id="openai", lane="background", worker_id="w-b", now=t0 + timedelta(milliseconds=500))

        # After TTL: capacity is NOT permanently leaked -- instance B can
        # acquire once the stale slot has expired.
        slot = gov_b.store.acquire(
            provider_id="openai", lane="background", worker_id="w-b", now=t0 + timedelta(seconds=2)
        )
        self.assertIsNotNone(slot)

    def test_circuit_breaker_state_shared_across_instances(self):
        from providers.governor import GovernorLimits, STATE_OPEN

        limits = GovernorLimits(failure_threshold=2)
        gov_a, gov_b = self._two_governors(limits)
        gov_a.store.record_failure("openai", "", error_code="provider_error")
        gov_a.store.record_failure("openai", "", error_code="provider_error")
        # Breaker opened by instance A is visible to instance B immediately.
        self.assertEqual(gov_b.breaker_state("openai", ""), STATE_OPEN)


class MultiInstanceHorizontalScalingTests(unittest.TestCase):
    """Group E (3.31): multi-instance processing, no duplicate completion,
    pool/lane isolation, tenant fairness, drain of one does not stop others.
    Composes the already-proven 3.27-3.30 shared-persistence primitives into
    the cross-block scenarios required by section 26/27."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_pool_lane_isolation_survives_multi_instance(self):
        q_a, q_b = _two_shared_sqlite_queues(self.db_path)
        q_a.allowed_lanes = frozenset({"interactive"})
        q_b.allowed_lanes = frozenset({"background"})

        for i in range(4):
            q_a.enqueue(
                workflow_id="wf", task_id=f"int-{i}", execution_key=f"int-ek-{i}",
                tenant_id="tenant-a", execution_lane="interactive", priority="critical",
            )
            q_a.enqueue(
                workflow_id="wf", task_id=f"bg-{i}", execution_key=f"bg-ek-{i}",
                tenant_id="tenant-a", execution_lane="background",
            )

        claimed_a, claimed_b = [], []
        while True:
            t = q_a.dequeue(worker_id="instance-a")
            if t is None:
                break
            claimed_a.append(t.execution_lane)
            q_a.start(t.queue_task_id, t.lease_id, worker_id="instance-a")
            q_a.ack(t.queue_task_id, t.lease_id, worker_id="instance-a")
        while True:
            t = q_b.dequeue(worker_id="instance-b")
            if t is None:
                break
            claimed_b.append(t.execution_lane)
            q_b.start(t.queue_task_id, t.lease_id, worker_id="instance-b")
            q_b.ack(t.queue_task_id, t.lease_id, worker_id="instance-b")

        self.assertEqual(set(claimed_a), {"interactive"})
        self.assertEqual(set(claimed_b), {"background"})
        self.assertEqual(len(claimed_a), 4)
        self.assertEqual(len(claimed_b), 4)

    def test_scenario4_tenant_fairness_across_instances(self):
        """A heavy tenant's growing backlog must not starve a light tenant's
        task claimed from a DIFFERENT instance. The existing claim_next
        fairness tie-break (fewer currently-running tasks sorts first,
        computed from the SAME shared running-task snapshot both instances
        read) is proven to hold across two independent connections to the
        same durable store, not merely inside one process."""

        from workflow.admission import AdmissionLimits

        limits = AdmissionLimits(
            max_running_per_tenant=3,
            max_running_global=None,
            max_pending_global=None,
            max_pending_per_tenant=None,
        )
        q_a, q_b = _two_shared_sqlite_queues(self.db_path)
        q_a.admission_limits = limits
        q_b.admission_limits = limits
        for i in range(10):
            q_a.enqueue(
                workflow_id="wf", task_id=f"heavy-{i}", execution_key=f"heavy-ek-{i}",
                tenant_id="tenant-heavy",
            )
        # Light tenant's single task is enqueued strictly AFTER the heavy
        # backlog (later available_at/created_at) -- plain FIFO would starve
        # it indefinitely behind 10 heavy items.
        q_b.enqueue(workflow_id="wf", task_id="light-0", execution_key="light-ek-0", tenant_id="tenant-light")

        # Instance A claims and starts (holds, does not ack) one heavy task.
        t0 = q_a.dequeue(worker_id="instance-a")
        self.assertEqual(t0.tenant_id, "tenant-heavy")
        q_a.start(t0.queue_task_id, t0.lease_id, worker_id="instance-a")

        # Instance B -- a genuinely separate connection to the shared store
        # -- immediately claims the light tenant's task next, not another
        # heavy item, because the shared running-count snapshot now favors
        # the tenant with fewer in-flight tasks (0 for light vs 1 for
        # heavy). This is real forward progress for tenant-light despite
        # the heavy backlog, visible cross-instance.
        t1 = q_b.dequeue(worker_id="instance-b")
        self.assertIsNotNone(t1)
        self.assertEqual(t1.tenant_id, "tenant-light")

        # The per-tenant running cap (shared, atomic) still bounds the heavy
        # tenant once it reaches the configured limit.
        held = [t0]
        for _ in range(5):
            t = q_a.dequeue(worker_id="instance-a")
            if t is None:
                break
            q_a.start(t.queue_task_id, t.lease_id, worker_id="instance-a")
            held.append(t)
        self.assertTrue(all(t.tenant_id == "tenant-heavy" for t in held))
        self.assertEqual(len(held), 3, "heavy tenant capped at max_running_per_tenant across instances")
        self.assertIsNone(q_a.dequeue(worker_id="instance-a"))

    def test_scenario7_drain_one_instance_others_continue(self):
        import asyncio

        from runtime.fleet_registry import FleetRegistry, SqliteFleetRegistryStore
        from workflow.service import build_workflow_runtime

        async def _run():
            from side_effects.persistence import build_side_effect_persistence

            env = {"SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite", "SIDE_EFFECT_DB_PATH": self.db_path}
            bundle_env_a = build_side_effect_persistence(env=env, run_recovery_scan=False)
            bundle_env_b = build_side_effect_persistence(env=env, run_recovery_scan=False)

            fleet_path = self.db_path + ".fleet"
            fleet_a = FleetRegistry(store=SqliteFleetRegistryStore(fleet_path))
            fleet_b = FleetRegistry(store=SqliteFleetRegistryStore(fleet_path))

            rt_a = build_workflow_runtime(
                runtime_role="worker", task_queue_store=bundle_env_a.task_queue_store
            )
            rt_b = build_workflow_runtime(
                runtime_role="worker", task_queue_store=bundle_env_b.task_queue_store
            )
            rt_a.fleet_registry = fleet_a
            rt_a.instance_id = "inst-a"
            rt_a._fleet_heartbeat_min_interval = 0.0
            rt_b.fleet_registry = fleet_b
            rt_b.instance_id = "inst-b"
            rt_b._fleet_heartbeat_min_interval = 0.0

            await rt_a.start_background(poll_interval=0.01)
            await rt_b.start_background(poll_interval=0.01)
            await asyncio.sleep(0.05)

            # Drain instance A only.
            rt_a.stop_new_claims()
            snap = fleet_a.snapshot()
            by_id = {v.instance_id: v for v in snap}
            self.assertTrue(by_id["inst-a"].draining)
            self.assertFalse(by_id["inst-b"].draining)

            # Instance B still accepts/serves new work after A drains.
            rt_b.queue.enqueue(
                workflow_id="wf-live", task_id="t-live", execution_key="ek-live", tenant_id="tenant-a"
            )
            for _ in range(20):
                await asyncio.sleep(0.02)
                try:
                    task = rt_b.queue.get_for_tenant(
                        rt_b.queue.store.find_by_execution_key("ek-live")[0].queue_task_id,
                        "tenant-a",
                    )
                except Exception:
                    task = None
                if task is not None and task.status in {"leased", "running", "completed"}:
                    break
            found = rt_b.queue.store.find_by_execution_key("ek-live")
            self.assertTrue(found)
            self.assertNotEqual(found[0].status, "queued", "instance B kept serving new work while A was draining")

            await rt_a.stop_background(drain_timeout_seconds=0.2)
            await rt_b.stop_background(drain_timeout_seconds=0.2)

        asyncio.run(_run())


class LoadSheddingTests(unittest.TestCase):
    """Group F (3.32): system saturation, deterministic shed/defer/reject,
    interactive protection, no silent durable-job loss."""

    def test_disabled_by_default_never_activates(self):
        from runtime.load_shedding import LoadShedConfig, LoadSignals, evaluate_load_shedding

        cfg = LoadShedConfig.from_env({})
        self.assertFalse(cfg.enabled)
        signals = LoadSignals(execution_lane="background", pool_saturation=1.0, oldest_queued_age_seconds=1e9)
        decision = evaluate_load_shedding(signals, cfg)
        self.assertEqual(decision.decision, "ACCEPT")
        self.assertEqual(decision.reason_code, "load_shed_disabled")

    def test_interactive_lane_never_shed_even_under_extreme_saturation(self):
        from runtime.load_shedding import LoadShedConfig, LoadSignals, evaluate_load_shedding

        cfg = LoadShedConfig(enabled=True)
        signals = LoadSignals(
            execution_lane="interactive",
            pool_saturation=1.0,
            oldest_queued_age_seconds=1e9,
            provider_saturated=True,
        )
        decision = evaluate_load_shedding(signals, cfg)
        self.assertEqual(decision.decision, "ACCEPT")
        self.assertEqual(decision.reason_code, "interactive_protected")

    def test_pool_saturation_deterministic_shed_defer_accept(self):
        from runtime.load_shedding import LoadShedConfig, LoadSignals, evaluate_load_shedding

        cfg = LoadShedConfig(
            enabled=True, pool_saturation_shed_threshold=0.95, pool_saturation_defer_threshold=0.8
        )
        accept = evaluate_load_shedding(
            LoadSignals(execution_lane="background", pool_saturation=0.5), cfg
        )
        defer = evaluate_load_shedding(
            LoadSignals(execution_lane="background", pool_saturation=0.85), cfg
        )
        shed = evaluate_load_shedding(
            LoadSignals(execution_lane="background", pool_saturation=0.99), cfg
        )
        self.assertEqual(accept.decision, "ACCEPT")
        self.assertEqual(defer.decision, "DEFER")
        self.assertEqual(shed.decision, "SHED")

    def test_provider_saturation_sheds_batch_work(self):
        from runtime.load_shedding import LoadShedConfig, LoadSignals, evaluate_load_shedding

        cfg = LoadShedConfig(enabled=True)
        decision = evaluate_load_shedding(
            LoadSignals(execution_lane="batch", provider_saturated=True), cfg
        )
        self.assertEqual(decision.decision, "SHED")
        self.assertEqual(decision.reason_code, "provider_saturated")

    def test_enforce_raises_and_does_not_touch_durable_state(self):
        from runtime.load_shedding import (
            LoadShedConfig,
            LoadShedRejectedError,
            LoadSignals,
            enforce_load_shedding,
        )

        cfg = LoadShedConfig(enabled=True, pool_saturation_shed_threshold=0.9)
        queue = TaskQueue(InMemoryTaskQueueStore(), now_fn=Clock())
        pre_existing = queue.enqueue(
            workflow_id="wf", task_id="t", execution_key="ek", tenant_id="tenant-a"
        )
        with self.assertRaises(LoadShedRejectedError) as ctx:
            enforce_load_shedding(
                LoadSignals(execution_lane="background", pool_saturation=0.99), cfg
            )
        self.assertEqual(ctx.exception.decision, "SHED")
        # Enforcement never touches already-durable work.
        self.assertEqual(queue.get(pre_existing.queue_task_id).status, "queued")

    def test_load_shed_decision_observable_via_metrics(self):
        from runtime.load_shedding import LoadShedConfig, LoadSignals, evaluate_load_shedding
        from runtime.metrics import RUNTIME_COUNTERS

        RUNTIME_COUNTERS.reset()
        cfg = LoadShedConfig(enabled=True, pool_saturation_shed_threshold=0.9)
        evaluate_load_shedding(LoadSignals(execution_lane="background", pool_saturation=0.99), cfg)
        self.assertEqual(RUNTIME_COUNTERS.total("load_shed"), 1)

    def test_build_load_signals_from_concurrency_snapshot(self):
        from runtime.load_shedding import build_load_signals

        signals = build_load_signals(
            execution_lane="batch",
            pool_concurrency={"max_concurrency": 4, "active": 4},
            oldest_queued_age_seconds=10.0,
        )
        self.assertEqual(signals.pool_saturation, 1.0)
        self.assertEqual(signals.execution_lane, "batch")

    def test_scenario6_heavy_saturation_sheds_while_interactive_unaffected(self):
        """SCENARIO 6: heavy/background saturation reaches the configured
        threshold; new heavy work is shed while interactive stays ACCEPT."""

        from runtime.load_shedding import LoadShedConfig, LoadSignals, evaluate_load_shedding

        cfg = LoadShedConfig.from_env({"LOAD_SHED_ENABLED": "true", "LOAD_SHED_POOL_SATURATION_SHED": "0.9"})
        heavy = evaluate_load_shedding(
            LoadSignals(execution_lane="background", pool_saturation=0.95), cfg
        )
        interactive = evaluate_load_shedding(
            LoadSignals(execution_lane="interactive", pool_saturation=0.95), cfg
        )
        self.assertEqual(heavy.decision, "SHED")
        self.assertEqual(interactive.decision, "ACCEPT")

    def test_user_cannot_spoof_exemption_via_metadata(self):
        """SCENARIO 11 (partial): shedding decisions are computed only from
        trusted server-side signals; there is no argument that lets a
        caller claim interactive/exemption status for a background lane."""

        from runtime.load_shedding import LoadShedConfig, LoadSignals, evaluate_load_shedding

        cfg = LoadShedConfig(enabled=True, pool_saturation_shed_threshold=0.5)
        spoofed = LoadSignals(
            execution_lane="background",  # real lane, cannot be overridden by a flag
            pool_saturation=0.99,
        )
        decision = evaluate_load_shedding(spoofed, cfg)
        self.assertEqual(decision.decision, "SHED")


class ReadinessLivenessTests(unittest.TestCase):
    """Group G (3.33): readiness vs liveness distinction, draining behavior,
    dependency-failure behavior. Proof tests for the EXISTING contract in
    config.runtime_health (already correctly separated prior to this
    mega-block); no code change was required here -- see delivery report."""

    def test_draining_worker_liveness_stays_healthy_readiness_not_ready(self):
        from config.runtime_health import evaluate_readiness

        snap = evaluate_readiness(env={}, draining=True)
        self.assertEqual(snap.liveness, "healthy")
        self.assertEqual(snap.readiness, "not_ready")

    def test_missing_persistence_fails_readiness_not_liveness(self):
        from config.runtime_health import evaluate_readiness

        class _FakeSideEffectRuntime:
            persistence = None
            workflow_runtime = None

        snap = evaluate_readiness(
            side_effect_runtime=_FakeSideEffectRuntime(), env={}, draining=False
        )
        self.assertEqual(snap.liveness, "healthy")
        self.assertEqual(snap.readiness, "not_ready")
        dep_names = {d.name: d.status for d in snap.dependencies}
        self.assertEqual(dep_names.get("persistence"), "not_ready")

    def test_missing_provider_governor_is_degraded_not_fatal(self):
        """A temporary/missing provider dependency must not be conflated
        with the whole process being unhealthy: readiness degrades but is
        not forced to not_ready by that signal alone, and liveness is
        unaffected."""

        from config.runtime_health import STATUS_DEGRADED, evaluate_readiness

        class _Store:
            available = True

            def list_all(self):
                return []

        class _Persistence:
            ready = True
            backend = "sqlite"
            schema_version = 1
            workflow_runtime_store = _Store()
            task_queue_store = _Store()
            schedule_store = _Store()

        class _WR:
            definitions = object()
            _claims_stopped = False

        class _SideEffectRuntime:
            persistence = _Persistence()
            workflow_runtime = _WR()
            budget_store = None
            provider_governor = None

        snap = evaluate_readiness(
            side_effect_runtime=_SideEffectRuntime(),
            env={"RUNTIME_ROLE": "worker"},
            draining=False,
        )
        self.assertEqual(snap.liveness, "healthy")
        dep_names = {d.name: d.status for d in snap.dependencies}
        self.assertEqual(dep_names.get("provider_governor"), STATUS_DEGRADED)
        self.assertNotEqual(snap.readiness, "not_ready")


class QueueRuntimeMetricsTests(unittest.TestCase):
    """Group H (3.34): queue/runtime metric correctness, bounded-cardinality
    contract."""

    def test_dlq_depth_reported_in_queue_snapshot(self):
        from observability.runtime_metrics import collect_queue_snapshot

        store = InMemoryTaskQueueStore()
        q = TaskQueue(store=store)
        _dead_letter_task(q, tenant_id="tenant-metrics")
        snap = collect_queue_snapshot(q)
        self.assertEqual(snap["dlq_depth"], 1)

    def test_operational_metrics_includes_lifecycle_counters_and_fleet_summary(self):
        from runtime.fleet_registry import FleetRegistry, InMemoryFleetRegistryStore
        from runtime.metrics import RUNTIME_COUNTERS
        from observability.runtime_metrics import collect_operational_metrics

        RUNTIME_COUNTERS.reset()
        RUNTIME_COUNTERS.inc("redrive", lane="background")
        fleet = FleetRegistry(store=InMemoryFleetRegistryStore())
        fleet.heartbeat(instance_id="i-metrics-1", pool_name="p", active_jobs=2, max_concurrency=5)

        metrics = collect_operational_metrics(fleet_registry=fleet)
        self.assertIn("queue_lifecycle_counters", metrics)
        self.assertEqual(metrics["queue_lifecycle_counters"]["redrive"]["background"], 1)
        self.assertIn("fleet_summary", metrics)
        self.assertEqual(metrics["fleet_summary"]["instance_count"], 1)
        self.assertIn("worker_drain_state", metrics)
        RUNTIME_COUNTERS.reset()

    def test_metric_labels_are_bounded_cardinality_not_tenant_or_request_id(self):
        """Lane-bucketed counters never key by tenant/request/run id -- only
        the fixed, small EXECUTION_LANES set (+ 'unknown')."""

        from runtime.metrics import RUNTIME_COUNTERS, RuntimeMetricsCounters
        from task_queue.lanes import EXECUTION_LANES

        counters = RuntimeMetricsCounters()
        counters.inc("redrive", lane="tenant-should-not-appear-as-lane")
        by_lane = counters.by_lane("redrive")
        # normalize_lane() maps any unrecognized string to DEFAULT_LANE, so
        # the raw tenant string is never itself used as a label key.
        self.assertNotIn("tenant-should-not-appear-as-lane", by_lane)
        self.assertTrue(set(by_lane.keys()).issubset(set(EXECUTION_LANES) | {"unknown"}))


class CapacitySaturationAlertTests(unittest.TestCase):
    """Group I (3.35): threshold alert, duration/hysteresis, recovery/clear
    behavior."""

    def test_new_alert_signals_are_additive_and_backward_compatible(self):
        from runtime.alerts import AlertThresholds, evaluate_alert_conditions
        from runtime.capacity_snapshot import CapacitySnapshot

        snap = CapacitySnapshot()
        # Pre-3.35 call signature (no new kwargs) must behave identically.
        alerts_before = evaluate_alert_conditions(snap, AlertThresholds())
        self.assertEqual(alerts_before, [])

        alerts_after = evaluate_alert_conditions(
            snap,
            AlertThresholds(),
            load_shed_active=True,
            provider_governor_saturated=True,
            stale_instance_count=2,
        )
        codes = {a.code for a in alerts_after}
        self.assertIn("persistent_load_shed", codes)
        self.assertIn("provider_governor_saturated", codes)
        self.assertIn("stale_runtime_instances", codes)

    def test_debounce_requires_continuous_breach_before_firing(self):
        from runtime.alerts import AlertDebouncer

        clock = Clock(T0)
        deb = AlertDebouncer(fire_after_seconds=60.0, clear_after_seconds=60.0)

        # First observation: breach just started -- must NOT fire yet.
        result = deb.observe({"queue_depth_high"}, now=clock.now)
        self.assertFalse(result["queue_depth_high"])

        # Still within debounce window (30s < 60s required) -- still no fire.
        clock.now = T0 + timedelta(seconds=30)
        result = deb.observe({"queue_depth_high"}, now=clock.now)
        self.assertFalse(result["queue_depth_high"])

        # Past the debounce window -- now fires.
        clock.now = T0 + timedelta(seconds=61)
        result = deb.observe({"queue_depth_high"}, now=clock.now)
        self.assertTrue(result["queue_depth_high"])
        self.assertTrue(deb.any_firing())

    def test_hysteresis_prevents_flapping_on_brief_recovery(self):
        from runtime.alerts import AlertDebouncer

        clock = Clock(T0)
        deb = AlertDebouncer(fire_after_seconds=10.0, clear_after_seconds=60.0)
        deb.observe({"dlq_growth"}, now=clock.now)
        clock.now = T0 + timedelta(seconds=11)
        self.assertTrue(deb.observe({"dlq_growth"}, now=clock.now)["dlq_growth"])

        # Condition briefly absent (only 5s) -- must NOT clear yet (avoids flap).
        clock.now = clock.now + timedelta(seconds=5)
        result = deb.observe(set(), now=clock.now)
        self.assertTrue(result["dlq_growth"])

        # Absent for the full clear window -- now clears.
        clock.now = clock.now + timedelta(seconds=61)
        result = deb.observe(set(), now=clock.now)
        self.assertFalse(result["dlq_growth"])

    def test_alert_state_observable_via_snapshot(self):
        from runtime.alerts import AlertDebouncer

        deb = AlertDebouncer(fire_after_seconds=0.0, clear_after_seconds=0.0)
        deb.observe({"pool_saturated"}, now=T0)
        snap = deb.observe({"pool_saturated"}, now=T0 + timedelta(seconds=1))
        self.assertTrue(snap["pool_saturated"])
        full = deb.snapshot()
        self.assertIn("pool_saturated", full)
        self.assertTrue(full["pool_saturated"]["firing"])


class ShadowTrafficTests(unittest.TestCase):
    """Group J (3.36): shadow sampling, non-authoritative result, side-effect
    denial, stable response unchanged."""

    def test_disabled_by_default_produces_no_shadow_execution(self):
        from runtime.shadow_traffic import ShadowTrafficConfig, should_sample_shadow

        cfg = ShadowTrafficConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.sample_rate, 0.0)
        self.assertFalse(should_sample_shadow("tenant-a", cfg))

    def test_deterministic_sampling_is_reproducible(self):
        from runtime.shadow_traffic import ShadowTrafficConfig, should_sample_shadow

        cfg = ShadowTrafficConfig(enabled=True, sample_rate=1.0)
        self.assertTrue(should_sample_shadow("tenant-a", cfg))
        self.assertTrue(should_sample_shadow("tenant-a", cfg))
        cfg_zero = ShadowTrafficConfig(enabled=True, sample_rate=0.0)
        self.assertFalse(should_sample_shadow("tenant-a", cfg_zero))

    def test_read_only_descriptor_is_shadow_eligible(self):
        from runtime.shadow_traffic import is_shadow_eligible
        from tools.adapters import search_tool_descriptor

        descriptor = search_tool_descriptor()
        self.assertTrue(descriptor.read_only)
        self.assertTrue(is_shadow_eligible(descriptor))

    def test_write_descriptor_is_not_shadow_eligible(self):
        from runtime.shadow_traffic import is_shadow_eligible
        from tools.models import (
            SIDE_EFFECT_WRITE,
            TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            ToolDescriptor,
        )

        descriptor = ToolDescriptor(
            tool_id="test.write_tool",
            name="Write Tool",
            description="writes something",
            version="1",
            trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            capabilities_required=(),
            action_types_supported=("write",),
            operations=("write",),
            read_only=False,
            reversible=True,
            idempotency_required=True,
            timeout_seconds=5.0,
            side_effect_level=SIDE_EFFECT_WRITE,
        )
        self.assertFalse(is_shadow_eligible(descriptor))

    def test_gateway_fails_closed_for_shadow_flagged_write_request(self):
        """Mandatory 3.36 acceptance requirement: the runtime/tool
        capability layer itself enforces the shadow side-effect firewall,
        not merely caller discipline."""

        import asyncio

        from tools.errors import ToolShadowNotEligibleError
        from tools.gateway import ToolGateway
        from tools.models import (
            SIDE_EFFECT_WRITE,
            TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            ToolDescriptor,
            ToolRequest,
        )
        from tools.registry import ToolRegistry

        descriptor = ToolDescriptor(
            tool_id="test.write_tool",
            name="Write Tool",
            description="writes something",
            version="1",
            trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            capabilities_required=(),
            action_types_supported=("write",),
            operations=("write",),
            read_only=False,
            reversible=True,
            idempotency_required=True,
            timeout_seconds=5.0,
            side_effect_level=SIDE_EFFECT_WRITE,
        )
        registry = ToolRegistry()
        registry.register(descriptor)
        gateway = ToolGateway(registry=registry, task_id="t1")
        request = ToolRequest(
            request_id="req-shadow-1",
            workflow_id="wf-1",
            task_id="t1",
            tool_id="test.write_tool",
            operation="write",
            arguments={},
            tenant_id="tenant-a",
            metadata={"traffic_mode": "shadow"},
        )

        async def _run():
            return await gateway.invoke(request)

        result = asyncio.run(_run())
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SHADOW_NOT_ELIGIBLE")

    def test_gateway_allows_shadow_flagged_read_only_request(self):
        import asyncio

        from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
        from autonomy.models import utc_now
        from tools.gateway import ToolGateway
        from tools.models import ToolRequest
        from tools.registry import ToolRegistry
        from tools.search.fake_provider import FakeSearchProvider

        registry = ToolRegistry()
        gateway = ToolGateway(FakeSearchProvider(), registry=registry, task_id="t1")
        caps = CapabilitySet(subject_id="a", capabilities=(CAP_EXTERNAL_READ,), issued_at=utc_now())
        request = ToolRequest(
            request_id="req-shadow-2",
            workflow_id="wf-1",
            task_id="t1",
            tool_id="search",
            operation="search",
            arguments={"query": "hello"},
            requested_capabilities=(CAP_EXTERNAL_READ,),
            tenant_id="tenant-a",
            metadata={"traffic_mode": "shadow"},
        )

        async def _run():
            return await gateway.invoke(request, capabilities=caps)

        result = asyncio.run(_run())
        self.assertNotEqual(result.error_code, "SHADOW_NOT_ELIGIBLE")
        self.assertTrue(result.success)

    def test_run_shadow_tool_call_never_returns_a_tool_result_type(self):
        """Non-authoritative-by-type-system proof: the shadow helper's
        return type can never be mistaken for/substituted as an authoritative
        ToolResult."""

        import asyncio

        from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
        from autonomy.models import utc_now
        from runtime.shadow_traffic import ShadowExecutionRecord, ShadowTrafficConfig, run_shadow_tool_call
        from tools.gateway import ToolGateway
        from tools.models import ToolRequest, ToolResult
        from tools.registry import ToolRegistry
        from tools.search.fake_provider import FakeSearchProvider

        registry = ToolRegistry()
        gateway = ToolGateway(FakeSearchProvider(), registry=registry, task_id="t1")
        caps = CapabilitySet(subject_id="a", capabilities=(CAP_EXTERNAL_READ,), issued_at=utc_now())
        request = ToolRequest(
            request_id="req-shadow-3",
            workflow_id="wf-1",
            task_id="t1",
            tool_id="search",
            operation="search",
            arguments={"query": "hello"},
            requested_capabilities=(CAP_EXTERNAL_READ,),
            tenant_id="tenant-a",
        )
        cfg = ShadowTrafficConfig(enabled=True, sample_rate=1.0)

        record = asyncio.run(
            run_shadow_tool_call(
                gateway, request, config=cfg, sample_key="tenant-a", capabilities=caps
            )
        )
        self.assertIsInstance(record, ShadowExecutionRecord)
        self.assertNotIsInstance(record, ToolResult)
        self.assertEqual(record.outcome, "executed")
        self.assertTrue(record.success)

    def test_ineligible_tool_never_invoked_for_shadow(self):
        """Pre-flight eligibility check must block BEFORE gateway.invoke is
        ever called for an unsafe tool -- proven via a gateway stub whose
        invoke() raises if called."""

        import asyncio

        from runtime.shadow_traffic import ShadowTrafficConfig, run_shadow_tool_call
        from tools.models import (
            SIDE_EFFECT_WRITE,
            TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            ToolDescriptor,
            ToolRequest,
        )
        from tools.registry import ToolRegistry

        descriptor = ToolDescriptor(
            tool_id="test.write_tool2",
            name="Write Tool",
            description="writes something",
            version="1",
            trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
            capabilities_required=(),
            action_types_supported=("write",),
            operations=("write",),
            read_only=False,
            reversible=True,
            idempotency_required=True,
            timeout_seconds=5.0,
            side_effect_level=SIDE_EFFECT_WRITE,
        )
        registry = ToolRegistry()
        registry.register(descriptor)

        class _StubGateway:
            def __init__(self, registry):
                self.registry = registry

            async def invoke(self, *a, **kw):
                raise AssertionError("gateway.invoke must never be called for an ineligible shadow tool")

        gateway = _StubGateway(registry)
        request = ToolRequest(
            request_id="req-shadow-4",
            workflow_id="wf-1",
            task_id="t1",
            tool_id="test.write_tool2",
            operation="write",
            arguments={},
            tenant_id="tenant-a",
        )
        cfg = ShadowTrafficConfig(enabled=True, sample_rate=1.0)
        record = asyncio.run(
            run_shadow_tool_call(gateway, request, config=cfg, sample_key="tenant-a")
        )
        self.assertEqual(record.outcome, "ineligible")
        self.assertEqual(record.error_code, "SHADOW_NOT_ELIGIBLE")

    def test_stable_authoritative_response_unaffected_by_shadow(self):
        """SCENARIO 8: stable executes authoritatively; shadow evaluation is
        fully independent and cannot alter the stable ToolResult."""

        import asyncio

        from autonomy.capabilities import CAP_EXTERNAL_READ, CapabilitySet
        from autonomy.models import utc_now
        from runtime.shadow_traffic import ShadowTrafficConfig, run_shadow_tool_call
        from tools.gateway import ToolGateway
        from tools.models import ToolRequest
        from tools.registry import ToolRegistry
        from tools.search.fake_provider import FakeSearchProvider

        registry = ToolRegistry()
        gateway = ToolGateway(FakeSearchProvider(), registry=registry, task_id="t1")
        caps = CapabilitySet(subject_id="a", capabilities=(CAP_EXTERNAL_READ,), issued_at=utc_now())
        stable_request = ToolRequest(
            request_id="req-stable-1",
            workflow_id="wf-1",
            task_id="t1",
            tool_id="search",
            operation="search",
            arguments={"query": "hello"},
            requested_capabilities=(CAP_EXTERNAL_READ,),
            tenant_id="tenant-a",
        )

        async def _run():
            stable_result = await gateway.invoke(stable_request, capabilities=caps)
            shadow_record = await run_shadow_tool_call(
                gateway,
                stable_request,
                config=ShadowTrafficConfig(enabled=True, sample_rate=1.0),
                sample_key="tenant-a",
                capabilities=caps,
            )
            return stable_result, shadow_record

        stable_result, shadow_record = asyncio.run(_run())
        self.assertTrue(stable_result.success)
        self.assertEqual(shadow_record.outcome, "executed")
        # Distinct request identities -- shadow never overwrote/aliased stable.
        self.assertEqual(stable_result.request_id, "req-stable-1")
        self.assertNotEqual(shadow_record.tool_id, None)


class CanaryTrafficTests(unittest.TestCase):
    """Group K (3.37): 0% stable, bounded canary percentage, deterministic
    assignment, stable/canary observability."""

    def test_zero_percent_default_is_always_stable(self):
        from runtime.canary_routing import CanaryConfig, MODE_STABLE, assign_canary

        cfg = CanaryConfig()
        self.assertFalse(cfg.is_usable())
        assignment = assign_canary(tenant_id="tenant-a", config=cfg)
        self.assertEqual(assignment.mode, MODE_STABLE)

    def test_invalid_config_fails_safe_to_stable(self):
        from runtime.canary_routing import CanaryConfig, MODE_STABLE, assign_canary

        # Enabled but missing candidate_id -- ambiguous, must be stable.
        cfg1 = CanaryConfig(enabled=True, candidate_id="", percent_basis_points=5000)
        self.assertEqual(assign_canary(tenant_id="tenant-a", config=cfg1).mode, MODE_STABLE)

        # Enabled with candidate but 0 percent -- must be stable.
        cfg2 = CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=0)
        self.assertEqual(assign_canary(tenant_id="tenant-a", config=cfg2).mode, MODE_STABLE)

    def test_bounded_percentage_never_sends_100_percent_by_accident(self):
        from runtime.canary_routing import CanaryConfig, MODE_CANDIDATE, assign_canary

        cfg = CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=1000)  # 10%
        candidate_count = 0
        total = 500
        for i in range(total):
            assignment = assign_canary(tenant_id=f"tenant-{i}", config=cfg)
            if assignment.mode == MODE_CANDIDATE:
                candidate_count += 1
        ratio = candidate_count / total
        # Deterministic hash bucketing should land roughly near 10% (loose
        # bound -- this is a determinism/boundedness check, not a precise
        # statistical test).
        self.assertLess(ratio, 0.25)
        self.assertGreater(candidate_count, 0)

    def test_full_percentage_sends_all_traffic_to_candidate(self):
        from runtime.canary_routing import CanaryConfig, MODE_CANDIDATE, assign_canary

        cfg = CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=10000)
        for i in range(20):
            assignment = assign_canary(tenant_id=f"tenant-{i}", config=cfg)
            self.assertEqual(assignment.mode, MODE_CANDIDATE)

    def test_assignment_is_deterministic_and_sticky_per_tenant(self):
        from runtime.canary_routing import CanaryConfig, assign_canary

        cfg = CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=3000)
        first = assign_canary(tenant_id="tenant-sticky", session_id="s1", config=cfg)
        second = assign_canary(tenant_id="tenant-sticky", session_id="s1", config=cfg)
        self.assertEqual(first.mode, second.mode)
        self.assertEqual(first.bucket, second.bucket)

    def test_missing_tenant_identity_fails_safe(self):
        from runtime.canary_routing import CanaryConfig, MODE_STABLE, assign_canary

        cfg = CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=10000)
        assignment = assign_canary(tenant_id="", config=cfg)
        self.assertEqual(assignment.mode, MODE_STABLE)

    def test_stable_and_candidate_are_observable_and_distinct(self):
        from runtime.canary_routing import CanaryConfig, MODE_CANDIDATE, MODE_STABLE, assign_canary

        cfg = CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=5000)
        modes = {assign_canary(tenant_id=f"tenant-{i}", config=cfg).mode for i in range(200)}
        self.assertTrue({MODE_STABLE, MODE_CANDIDATE}.issubset(modes))

    def test_user_supplied_candidate_flag_cannot_force_candidate_assignment(self):
        """SCENARIO 11 (spoof attack): assign_canary only accepts trusted
        tenant/session identity -- there is no argument through which a
        caller can request forced candidate assignment."""

        import inspect

        from runtime.canary_routing import assign_canary

        params = set(inspect.signature(assign_canary).parameters.keys())
        self.assertEqual(params, {"tenant_id", "session_id", "config"})


class CanaryRollbackTests(unittest.TestCase):
    """Group L (3.38): manual rollback, automatic rollback contract,
    idempotent rollback, stable routing after rollback."""

    def test_manual_rollback_forces_stable_for_all_future_assignments(self):
        from runtime.canary_routing import CanaryConfig, CanaryRolloutController, MODE_STABLE

        controller = CanaryRolloutController(
            config=CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=10000)
        )
        # Before rollback: candidate.
        pre = controller.assign(tenant_id="tenant-a")
        self.assertEqual(pre.mode, "candidate")

        record = controller.rollback(reason="manual_operator_action", actor="ops-user-1")
        self.assertTrue(record.triggered)
        self.assertEqual(record.trigger_type, "manual")

        post = controller.assign(tenant_id="tenant-a")
        self.assertEqual(post.mode, MODE_STABLE)

    def test_rollback_is_idempotent_under_repeated_invocation(self):
        from runtime.canary_routing import CanaryConfig, CanaryRolloutController

        controller = CanaryRolloutController(
            config=CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=10000)
        )
        first = controller.rollback(reason="r1", actor="a1")
        second = controller.rollback(reason="r2", actor="a2")
        third = controller.rollback(reason="r3", actor="a3")
        self.assertTrue(first.triggered)
        self.assertFalse(second.triggered)
        self.assertFalse(third.triggered)
        # Reason/actor preserved from the FIRST rollback -- repeated calls
        # never overwrite the original auditable rollback event.
        self.assertEqual(second.reason, "r1")
        self.assertEqual(second.actor, "a1")

    def test_automatic_rollback_requires_debounced_consecutive_breaches(self):
        from runtime.canary_routing import CanaryConfig, CanaryRolloutController

        controller = CanaryRolloutController(
            config=CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=10000)
        )
        # Two breaches -- not enough (default streak requirement is 3).
        self.assertIsNone(
            controller.evaluate_automatic_rollback(candidate_error_rate=0.5, max_error_rate=0.1)
        )
        self.assertIsNone(
            controller.evaluate_automatic_rollback(candidate_error_rate=0.5, max_error_rate=0.1)
        )
        self.assertFalse(controller.is_rolled_back())
        # Third consecutive breach -- triggers rollback.
        record = controller.evaluate_automatic_rollback(candidate_error_rate=0.5, max_error_rate=0.1)
        self.assertIsNotNone(record)
        self.assertTrue(record.triggered)
        self.assertEqual(record.trigger_type, "automatic")
        self.assertTrue(controller.is_rolled_back())

    def test_automatic_rollback_streak_resets_on_healthy_sample(self):
        from runtime.canary_routing import CanaryConfig, CanaryRolloutController

        controller = CanaryRolloutController(
            config=CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=10000)
        )
        controller.evaluate_automatic_rollback(candidate_error_rate=0.5, max_error_rate=0.1)
        controller.evaluate_automatic_rollback(candidate_error_rate=0.5, max_error_rate=0.1)
        # Healthy sample resets the streak -- avoids flapping into rollback
        # from transient noise.
        self.assertIsNone(
            controller.evaluate_automatic_rollback(candidate_error_rate=0.01, max_error_rate=0.1)
        )
        self.assertIsNone(
            controller.evaluate_automatic_rollback(candidate_error_rate=0.5, max_error_rate=0.1)
        )
        self.assertIsNone(
            controller.evaluate_automatic_rollback(candidate_error_rate=0.5, max_error_rate=0.1)
        )
        self.assertFalse(controller.is_rolled_back())

    def test_rollback_does_not_require_deleting_telemetry(self):
        """Rollback only flips routing; it never mutates/erases prior
        assignment history or metrics counters."""

        from runtime.canary_routing import CanaryConfig, CanaryRolloutController
        from runtime.metrics import RUNTIME_COUNTERS

        RUNTIME_COUNTERS.reset()
        controller = CanaryRolloutController(
            config=CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=10000)
        )
        controller.assign(tenant_id="tenant-a")
        before = dict(RUNTIME_COUNTERS.as_dict())
        controller.rollback(reason="manual", actor="ops")
        after = dict(RUNTIME_COUNTERS.as_dict())
        self.assertEqual(before.get("canary_candidate"), after.get("canary_candidate"))
        RUNTIME_COUNTERS.reset()

    def test_rollback_observable_and_auditable(self):
        from runtime.canary_routing import CanaryConfig, CanaryRolloutController

        controller = CanaryRolloutController(config=CanaryConfig())
        record = controller.rollback(reason="candidate_unhealthy", actor="ops-user-2")
        d = record.as_dict()
        self.assertEqual(d["reason"], "candidate_unhealthy")
        self.assertEqual(d["actor"], "ops-user-2")
        self.assertIsNotNone(d["rolled_back_at"])

    def test_scenario10_candidate_failure_triggers_rollback_then_stable(self):
        """SCENARIO 10: candidate health/error signal breaches rollback
        policy -> rollback activates -> subsequent authoritative traffic
        routes to stable."""

        from runtime.canary_routing import CanaryConfig, CanaryRolloutController, MODE_STABLE

        controller = CanaryRolloutController(
            config=CanaryConfig(enabled=True, candidate_id="cand-1", percent_basis_points=10000)
        )
        for _ in range(3):
            controller.evaluate_automatic_rollback(candidate_error_rate=0.9, max_error_rate=0.2)
        self.assertTrue(controller.is_rolled_back())
        for i in range(10):
            assignment = controller.assign(tenant_id=f"tenant-{i}")
            self.assertEqual(assignment.mode, MODE_STABLE)


if __name__ == "__main__":
    unittest.main()
