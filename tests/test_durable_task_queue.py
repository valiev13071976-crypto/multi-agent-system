"""Phase 3 Patch 1 — durable TaskQueue atomic claim / lease / heartbeat."""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from side_effects.persistence import build_side_effect_persistence
from side_effects.schema import SCHEMA_VERSION
from side_effects.sqlite_store import SqliteConnection
from task_queue.errors import QueueLeaseError
from task_queue.models import STATUS_COMPLETED, STATUS_LEASED, STATUS_QUEUED, STATUS_RETRY_WAIT, STATUS_RUNNING, utc_now
from task_queue.queue import TaskQueue
from task_queue.sqlite_store import PersistentTaskQueueStore
from task_queue.worker import TaskWorker
from tests.test_workflow_foundation import linear_demo_definition
from workflow.definition import STEP_TYPE_HANDLER, StepResult
from workflow.models import STATUS_QUEUED as WF_QUEUED
from workflow.service import build_workflow_runtime
from workflow.state_manager import StateManager


def _env(path: str) -> dict:
    return {
        "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
        "SIDE_EFFECT_DB_PATH": path,
        "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
    }


class SchemaV5QueueMigrationTests(unittest.TestCase):
    def test_v4_migrates_to_v5_preserves_schedules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "v4.sqlite3")
            # Build v4-shaped DB then bump via initialize_schema
            conn = SqliteConnection(path)
            # Force meta to 4 after full init, then re-run as if upgrading
            version = conn.initialize_schema()
            self.assertEqual(version, SCHEMA_VERSION)
            conn.connect().execute(
                "UPDATE side_effect_schema_meta SET version = 4 WHERE id = 1"
            )
            conn.connect().commit()
            # Seed a schedule row
            now = utc_now().isoformat()
            conn.connect().execute(
                """
                INSERT INTO workflow_schedules (
                    schedule_id, tenant_id, workflow_type, version, payload_json,
                    next_run_at, interval_seconds, enabled, run_count,
                    created_at, updated_at, row_version
                ) VALUES (?, ?, ?, ?, '{}', ?, 60, 1, 0, ?, ?, 1)
                """,
                ("sched-keep", "tenant-A", "demo.linear", "1", now, now, now),
            )
            conn.connect().commit()
            conn.close()

            conn2 = SqliteConnection(path)
            v2 = conn2.initialize_schema()
            self.assertEqual(v2, SCHEMA_VERSION)
            tables = {
                str(r["name"])
                for r in conn2.connect()
                .execute("SELECT name FROM sqlite_master WHERE type='table'")
                .fetchall()
            }
            self.assertIn("queue_tasks", tables)
            row = conn2.connect().execute(
                "SELECT schedule_id, tenant_id FROM workflow_schedules WHERE schedule_id=?",
                ("sched-keep",),
            ).fetchone()
            self.assertEqual(row["tenant_id"], "tenant-A")
            # WAL / busy_timeout configured on connect
            mode = conn2.connect().execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
            conn2.close()


class DurableQueueRestartTests(unittest.TestCase):
    def test_enqueue_survives_store_recreate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "q.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            self.assertEqual(bundle.schema_version, SCHEMA_VERSION)
            q1 = TaskQueue(store=bundle.task_queue_store, lease_seconds=60)
            task = q1.enqueue(
                workflow_id="wf1",
                task_id="t1",
                execution_key="ek-durable-1",
                tenant_id="tenant-A",
                user_id="u1",
                actor_ref="tenant-A:u1",
            )
            if bundle.connection is not None:
                bundle.connection.close()

            bundle2 = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            try:
                q2 = TaskQueue(store=bundle2.task_queue_store)
                got = q2.get(task.queue_task_id)
                self.assertEqual(got.status, STATUS_QUEUED)
                self.assertEqual(got.tenant_id, "tenant-A")
                self.assertEqual(got.user_id, "u1")
                self.assertEqual(got.actor_ref, "tenant-A:u1")
            finally:
                if bundle2.connection is not None:
                    bundle2.connection.close()


class AtomicClaimTests(unittest.TestCase):
    def test_two_workers_one_task_exactly_one_claim(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "claim.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            store = bundle.task_queue_store
            self.assertIsInstance(store, PersistentTaskQueueStore)
            try:
                q = TaskQueue(store=store, lease_seconds=60)
                q.enqueue(
                    workflow_id="wf",
                    task_id="t",
                    execution_key="ek-one",
                    tenant_id="tenant-A",
                )

                results = []
                barrier = threading.Barrier(2)

                def worker(name: str):
                    barrier.wait()
                    local = TaskQueue(store=store, lease_seconds=60)
                    claimed = local.dequeue(worker_id=name)
                    results.append(
                        (
                            name,
                            claimed.queue_task_id if claimed else None,
                            claimed.lease_id if claimed else None,
                        )
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    f1 = pool.submit(worker, "worker-A")
                    f2 = pool.submit(worker, "worker-B")
                    f1.result()
                    f2.result()

                claimed = [r for r in results if r[1] is not None]
                self.assertEqual(len(claimed), 1)
                owners = {r[0] for r in claimed}
                self.assertEqual(len(owners), 1)
            finally:
                if bundle.connection is not None:
                    bundle.connection.close()

    def test_n_tasks_two_workers_no_duplicate_ownership(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "multi.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            store = bundle.task_queue_store
            try:
                q = TaskQueue(store=store, lease_seconds=60)
                n = 8
                for i in range(n):
                    q.enqueue(
                        workflow_id=f"wf-{i}",
                        task_id=f"t-{i}",
                        execution_key=f"ek-{i}",
                        tenant_id="tenant-A",
                    )

                claimed_ids: list[str] = []
                lock = threading.Lock()
                barrier = threading.Barrier(2)

                def drain(name: str):
                    barrier.wait()
                    local = TaskQueue(store=store, lease_seconds=60)
                    while True:
                        task = local.dequeue(worker_id=name)
                        if task is None:
                            break
                        with lock:
                            claimed_ids.append(task.queue_task_id)

                with ThreadPoolExecutor(max_workers=2) as pool:
                    f1 = pool.submit(drain, "wa")
                    f2 = pool.submit(drain, "wb")
                    f1.result()
                    f2.result()

                self.assertEqual(len(claimed_ids), n)
                self.assertEqual(len(set(claimed_ids)), n)
            finally:
                if bundle.connection is not None:
                    bundle.connection.close()


class LeaseHeartbeatTests(unittest.TestCase):
    def test_heartbeat_extends_and_blocks_reclaim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "hb.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(store=bundle.task_queue_store, lease_seconds=5)
            q.enqueue(
                workflow_id="wf", task_id="t", execution_key="ek-hb", tenant_id="tA"
            )
            claimed = q.dequeue(worker_id="A")
            q.start(claimed.queue_task_id, claimed.lease_id, worker_id="A")
            before = q.get(claimed.queue_task_id).lease_expires_at
            renewed = q.heartbeat(claimed.queue_task_id, "A", claimed.lease_id)
            self.assertGreaterEqual(renewed.lease_expires_at, before)
            # B cannot reclaim while lease live
            self.assertEqual(q.recover_stuck_running(), ())
            other = TaskQueue(store=bundle.task_queue_store, lease_seconds=5)
            self.assertIsNone(other.dequeue(worker_id="B"))
            if bundle.connection is not None:
                bundle.connection.close()

    def test_expiry_allows_new_lease_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "exp.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(store=bundle.task_queue_store, lease_seconds=30)
            q.enqueue(
                workflow_id="wf", task_id="t", execution_key="ek-exp", tenant_id="tA"
            )
            a = q.dequeue(worker_id="A")
            q.start(a.queue_task_id, a.lease_id, worker_id="A")
            old_lease = a.lease_id
            q.store.save(
                replace(
                    q.get(a.queue_task_id),
                    lease_expires_at=utc_now() - timedelta(seconds=1),
                )
            )
            reclaimed = q.recover_stuck_running()
            self.assertEqual(list(reclaimed), [a.queue_task_id])
            b = q.dequeue(worker_id="B")
            self.assertIsNotNone(b)
            self.assertEqual(b.worker_id, "B")
            self.assertNotEqual(b.lease_id, old_lease)
            if bundle.connection is not None:
                bundle.connection.close()

    def test_stale_ack_rejected_after_reclaim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "stale.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(store=bundle.task_queue_store, lease_seconds=30)
            q.enqueue(
                workflow_id="wf", task_id="t", execution_key="ek-stale", tenant_id="tA"
            )
            a = q.dequeue(worker_id="A")
            q.start(a.queue_task_id, a.lease_id, worker_id="A")
            old_lease = a.lease_id
            q.store.save(
                replace(
                    q.get(a.queue_task_id),
                    lease_expires_at=utc_now() - timedelta(seconds=1),
                )
            )
            q.recover_stuck_running()
            b = q.dequeue(worker_id="B")
            q.start(b.queue_task_id, b.lease_id, worker_id="B")
            with self.assertRaises(QueueLeaseError):
                q.ack(a.queue_task_id, old_lease, worker_id="A")
            with self.assertRaises(QueueLeaseError):
                q.fail(
                    a.queue_task_id,
                    old_lease,
                    error_code="boom",
                    worker_id="A",
                )
            cur = q.get(b.queue_task_id)
            self.assertEqual(cur.worker_id, "B")
            self.assertEqual(cur.lease_id, b.lease_id)
            self.assertEqual(cur.status, STATUS_RUNNING)
            if bundle.connection is not None:
                bundle.connection.close()


class ForeignLeaseStartupTests(unittest.TestCase):
    def test_valid_foreign_lease_not_startup_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "foreign.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q1 = TaskQueue(store=bundle.task_queue_store, lease_seconds=300)
            q1.enqueue(
                workflow_id="wf", task_id="t", execution_key="ek-f", tenant_id="tA"
            )
            claimed = q1.dequeue(worker_id="worker-live")
            q1.start(claimed.queue_task_id, claimed.lease_id, worker_id="worker-live")
            live_until = q1.get(claimed.queue_task_id).lease_expires_at

            # Simulate another runtime starting against same DB
            q2 = TaskQueue(store=bundle.task_queue_store, lease_seconds=300)
            self.assertEqual(q2.recover_stuck_running(force=True), ())
            got = q2.get(claimed.queue_task_id)
            self.assertEqual(got.status, STATUS_RUNNING)
            self.assertEqual(got.worker_id, "worker-live")
            self.assertEqual(got.lease_expires_at, live_until)

            # Expired still reclaimable after "restart"
            q2.store.save(
                replace(got, lease_expires_at=utc_now() - timedelta(seconds=2))
            )
            self.assertEqual(list(q2.recover_stuck_running(force=True)), [claimed.queue_task_id])
            if bundle.connection is not None:
                bundle.connection.close()


class WorkflowRecoveryInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_reconstructs_missing_queue_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "wfrec.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            sm = StateManager(store=bundle.workflow_runtime_store)
            runtime = build_workflow_runtime(
                state_manager=sm,
                schedule_store=bundle.schedule_store,
                task_queue_store=bundle.task_queue_store,
            )
            runtime.definitions.register(linear_demo_definition())
            runtime.platform.register_handler(
                STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
            )
            created = await runtime.create_and_enqueue(
                "demo.linear", "1", tenant_id="tenant-A", execution_key="rec-ek-1"
            )
            wid = created["workflow_id"]
            qid = created["queue_task_id"]
            # Simulate lost queue row while workflow remains queued
            conn = bundle.connection.connect()
            conn.execute("DELETE FROM queue_tasks WHERE queue_task_id = ?", (qid,))
            conn.commit()
            self.assertIsNone(runtime.queue.store.get(qid))
            self.assertEqual(runtime.state_manager.get(wid).status, WF_QUEUED)

            report = runtime.recover_and_reenqueue_persisted()
            self.assertIn(wid, report["reenqueued"])
            active = runtime.queue.store.find_by_execution_key(
                created["execution_key"]
                if ":" in str(created.get("execution_key"))
                else f"tenant-A:{created.get('execution_key', 'rec-ek-1')}"
            )
            # Scoped key used internally
            from security.tenant import scope_execution_key

            scoped = scope_execution_key("tenant-A", "rec-ek-1")
            found = runtime.queue.store.find_by_execution_key(scoped)
            self.assertTrue(found)
            self.assertEqual(len([t for t in found if t.status in {"queued", "leased", "running", "retry_wait"}]), 1)
            if bundle.connection is not None:
                bundle.connection.close()

    async def test_recovery_no_duplicate_when_queue_row_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "nodupe.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            sm = StateManager(store=bundle.workflow_runtime_store)
            runtime = build_workflow_runtime(
                state_manager=sm,
                task_queue_store=bundle.task_queue_store,
            )
            runtime.definitions.register(linear_demo_definition())
            runtime.platform.register_handler(
                STEP_TYPE_HANDLER, lambda ctx: StepResult(ok=True, data={})
            )
            created = await runtime.create_and_enqueue(
                "demo.linear", "1", tenant_id="tenant-A", execution_key="nodupe-ek"
            )
            from security.tenant import scope_execution_key

            scoped = scope_execution_key("tenant-A", "nodupe-ek")
            before = runtime.queue.store.find_by_execution_key(scoped)
            self.assertEqual(len(before), 1)
            qid = before[0].queue_task_id

            report = runtime.recover_and_reenqueue_persisted()
            after = runtime.queue.store.find_by_execution_key(scoped)
            self.assertEqual(len(after), 1)
            self.assertEqual(after[0].queue_task_id, qid)
            # Still only one active row
            self.assertEqual(
                len([t for t in after if t.status != STATUS_COMPLETED]), 1
            )
            if bundle.connection is not None:
                bundle.connection.close()


class DurableDlqTests(unittest.TestCase):
    def test_dead_letter_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "dlq.sqlite3")
            bundle = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            q = TaskQueue(store=bundle.task_queue_store, lease_seconds=30)
            q.enqueue(
                workflow_id="wf",
                task_id="t",
                execution_key="ek-dlq",
                tenant_id="tA",
                max_attempts=1,
            )
            claimed = q.dequeue(worker_id="A")
            q.start(claimed.queue_task_id, claimed.lease_id, worker_id="A")
            q.fail(
                claimed.queue_task_id,
                claimed.lease_id,
                error_code="permanent_boom",
                worker_id="A",
            )
            qid = claimed.queue_task_id
            if bundle.connection is not None:
                bundle.connection.close()

            bundle2 = build_side_effect_persistence(
                env=_env(path), durable=True, run_recovery_scan=False
            )
            try:
                q2 = TaskQueue(store=bundle2.task_queue_store)
                letters = q2.get_dead_letters()
                self.assertEqual(len(letters), 1)
                self.assertEqual(letters[0].queue_task_id, qid)
                self.assertEqual(letters[0].error_code, "permanent_boom")
                self.assertEqual(letters[0].tenant_id, "tA")
            finally:
                if bundle2.connection is not None:
                    bundle2.connection.close()


if __name__ == "__main__":
    unittest.main()
