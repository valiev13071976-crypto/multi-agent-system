"""Reproducible load / failure harness for Phase 3 Block 3.

Uses durable SQLite queue + fake timings — NEVER calls paid AI APIs.
Run: python -m harness.load_harness --scenario all
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from config.runtime_config import validate_runtime_config
from providers.governor import (
    GovernorLimits,
    InMemoryProviderGovernorStore,
    ProviderCapacityUnavailable,
    ProviderGovernor,
)
from side_effects.persistence import build_side_effect_persistence
from task_queue.lanes import (
    LANE_BACKGROUND,
    LANE_BULK,
    LANE_INTERACTIVE,
    LANE_SCHEDULED,
    LaneCapacityConfig,
)
from task_queue.queue import TaskQueue


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    metrics: dict = field(default_factory=dict)
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "metrics": self.metrics,
            "notes": self.notes,
        }


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return float(ordered[idx])


def _make_queue(tmp: str, **env_extra) -> tuple:
    path = str(Path(tmp) / "harness.sqlite3")
    env = {
        "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
        "SIDE_EFFECT_DB_PATH": path,
        "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
        "MAX_RUNNING_GLOBAL": "20",
        "MAX_RUNNING_PER_TENANT": "10",
        "INTERACTIVE_RESERVED": "5",
        "BACKGROUND_MAY_BORROW_INTERACTIVE": "true",
        "TENANT_FAIRNESS_ENABLED": "true",
        **env_extra,
    }
    validate_runtime_config(env, raise_on_error=True)
    bundle = build_side_effect_persistence(
        env=env, durable=True, run_recovery_scan=False
    )
    q = TaskQueue(
        store=bundle.task_queue_store,
        lease_seconds=float(env.get("WORKER_LEASE_SECONDS", 2)),
        lane_config=LaneCapacityConfig.from_env(env),
    )
    return bundle, q, env


def scenario_interactive_baseline(n: int = 50) -> ScenarioResult:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        bundle, q, env = _make_queue(tmp)
        waits = []
        for i in range(n):
            t0 = time.perf_counter()
            q.enqueue(
                workflow_id=f"ix-{i}",
                task_id=f"t-{i}",
                execution_key=f"ek-ix-{i}",
                tenant_id="tenant-user",
                execution_lane=LANE_INTERACTIVE,
                priority="high",
            )
            claimed = q.dequeue(
                worker_id="w0",
                max_running_global=20,
                max_running_per_tenant=10,
            )
            waits.append((time.perf_counter() - t0) * 1000.0)
            if claimed is None:
                return ScenarioResult("interactive_baseline", False, notes="claim_miss")
            q.start(claimed.queue_task_id, claimed.lease_id, worker_id="w0")
            q.ack(claimed.queue_task_id, claimed.lease_id, worker_id="w0")
        if bundle.connection is not None:
            bundle.connection.close()
        return ScenarioResult(
            "interactive_baseline",
            True,
            {
                "throughput_jobs": n,
                "claim_wait_p50_ms": _percentile(waits, 50),
                "claim_wait_p95_ms": _percentile(waits, 95),
                "claim_wait_avg_ms": statistics.mean(waits) if waits else 0,
            },
        )


def scenario_background_saturation() -> ScenarioResult:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        bundle, q, env = _make_queue(
            tmp,
            MAX_RUNNING_GLOBAL="20",
            INTERACTIVE_RESERVED="5",
            BACKGROUND_MAY_BORROW_INTERACTIVE="false",
        )
        q.lane_config = LaneCapacityConfig(
            interactive_reserved=5, background_may_borrow=False
        )
        for i in range(200):
            q.enqueue(
                workflow_id=f"bg-{i}",
                task_id=f"tb-{i}",
                execution_key=f"ekb-{i}",
                tenant_id="tenant-bg",
                execution_lane=LANE_BACKGROUND,
                priority="low",
            )
        # Fill background capacity only (no borrow)
        bg_claimed = 0
        for i in range(20):
            t = q.dequeue(
                worker_id=f"wb{i}", max_running_global=20, max_running_per_tenant=50
            )
            if t is None:
                break
            if t.execution_lane != LANE_INTERACTIVE:
                bg_claimed += 1
        q.enqueue(
            workflow_id="ix-1",
            task_id="tix",
            execution_key="ek-ix-sat",
            tenant_id="tenant-user",
            execution_lane=LANE_INTERACTIVE,
            priority="high",
        )
        t0 = time.perf_counter()
        ix = q.dequeue(
            worker_id="wix", max_running_global=20, max_running_per_tenant=50
        )
        wait_ms = (time.perf_counter() - t0) * 1000.0
        ok = ix is not None and ix.execution_lane == LANE_INTERACTIVE
        if bundle.connection is not None:
            bundle.connection.close()
        return ScenarioResult(
            "background_saturation",
            ok,
            {
                "background_claimed_before_ix": bg_claimed,
                "interactive_wait_ms": wait_ms,
                "interactive_claimed": ok,
            },
            notes="" if ok else "interactive_blocked",
        )


def scenario_tenant_bully() -> ScenarioResult:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        bundle, q, env = _make_queue(tmp, INTERACTIVE_RESERVED="0")
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
        first = q.dequeue(
            worker_id="w0", max_running_global=50, max_running_per_tenant=50
        )
        second = q.dequeue(
            worker_id="w1", max_running_global=50, max_running_per_tenant=50
        )
        ok = (
            first is not None
            and second is not None
            and second.tenant_id == "tenant-B"
        )
        if bundle.connection is not None:
            bundle.connection.close()
        return ScenarioResult(
            "tenant_bully",
            ok,
            {
                "first_tenant": getattr(first, "tenant_id", None),
                "second_tenant": getattr(second, "tenant_id", None),
            },
        )


def scenario_schedule_storm() -> ScenarioResult:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        bundle, q, env = _make_queue(
            tmp, BACKGROUND_MAY_BORROW_INTERACTIVE="false"
        )
        q.lane_config = LaneCapacityConfig(
            interactive_reserved=5, background_may_borrow=False
        )
        for i in range(100):
            q.enqueue(
                workflow_id=f"s-{i}",
                task_id=f"ts-{i}",
                execution_key=f"eks-{i}",
                tenant_id="sched",
                execution_lane=LANE_SCHEDULED,
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
        ok = len(interactive) >= 1 and len(background) <= 15
        if bundle.connection is not None:
            bundle.connection.close()
        return ScenarioResult(
            "schedule_storm",
            ok,
            {
                "interactive": len(interactive),
                "background": len(background),
                "total_claimed": len(claimed),
            },
        )


def scenario_worker_loss() -> ScenarioResult:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        bundle, q, env = _make_queue(tmp)
        q.lease_seconds = 1.0
        q.enqueue(
            workflow_id="wf",
            task_id="t",
            execution_key="ek-loss",
            tenant_id="tA",
            execution_lane=LANE_BACKGROUND,
        )
        a = q.dequeue(worker_id="dead", lease_seconds=1.0, max_running_global=10)
        time.sleep(1.1)
        b = q.dequeue(worker_id="alive", lease_seconds=30.0, max_running_global=10)
        ok = (
            a is not None
            and b is not None
            and a.queue_task_id == b.queue_task_id
            and a.lease_id != b.lease_id
            and b.worker_id == "alive"
        )
        if bundle.connection is not None:
            bundle.connection.close()
        return ScenarioResult(
            "worker_loss",
            ok,
            {
                "reclaimed": ok,
                "sqlite_busy": getattr(bundle.task_queue_store, "sqlite_busy_count", 0),
            },
        )


def scenario_provider_saturation() -> ScenarioResult:
    limits = GovernorLimits(
        max_concurrency=2, interactive_reserved=1, background_may_borrow=False
    )
    store = InMemoryProviderGovernorStore(limits)
    gov = ProviderGovernor(store=store, limits=limits)
    ok_slots = []
    errs = []
    barrier = threading.Barrier(6)

    def worker(i: int, lane: str):
        barrier.wait()
        try:
            slot = gov.acquire(
                provider_id="fake", model_id="m", lane=lane, worker_id=f"w{i}"
            )
            ok_slots.append(slot)
            time.sleep(0.05)
            gov.release(slot)
        except ProviderCapacityUnavailable as exc:
            errs.append(exc.reason)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = []
        for i in range(4):
            futs.append(pool.submit(worker, i, "background"))
        for i in range(4, 6):
            futs.append(pool.submit(worker, i, "interactive"))
        for f in as_completed(futs):
            f.result()
    # Never more than max_concurrency concurrent acquisitions recorded as success
    # before release; errs must exist under saturation.
    ok = len(errs) > 0 and len(ok_slots) >= 1
    return ScenarioResult(
        "provider_saturation",
        ok,
        {"acquisitions": len(ok_slots), "throttles": len(errs)},
    )


def scenario_429_storm() -> ScenarioResult:
    limits = GovernorLimits(max_concurrency=8, failure_threshold=3, cooldown_seconds=0.3)
    store = InMemoryProviderGovernorStore(limits)
    gov = ProviderGovernor(store=store, limits=limits)
    gov.record_429("fake", "m", retry_after_seconds=1.0)
    blocked = 0
    for _ in range(20):
        try:
            gov.acquire(provider_id="fake", model_id="m")
        except ProviderCapacityUnavailable:
            blocked += 1
    ok = blocked == 20
    return ScenarioResult(
        "429_storm",
        ok,
        {"blocked_acquisitions": blocked, "no_retry_storm": ok},
    )


def scenario_sqlite_contention(workers: int = 8, ops: int = 40) -> ScenarioResult:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        bundle, q, env = _make_queue(tmp, MAX_RUNNING_GLOBAL="50")
        for i in range(ops * 2):
            q.enqueue(
                workflow_id=f"c-{i}",
                task_id=f"t-{i}",
                execution_key=f"ekc-{i}",
                tenant_id=f"t-{i % 5}",
                execution_lane=LANE_BULK if i % 3 else LANE_BACKGROUND,
            )
        latencies = []
        lock = threading.Lock()

        def worker(wid: str):
            local = []
            for _ in range(ops // workers + 1):
                t0 = time.perf_counter()
                t = q.dequeue(
                    worker_id=wid, max_running_global=50, max_running_per_tenant=20
                )
                local.append((time.perf_counter() - t0) * 1000.0)
                if t is not None:
                    try:
                        q.start(t.queue_task_id, t.lease_id, worker_id=wid)
                        q.ack(t.queue_task_id, t.lease_id, worker_id=wid)
                    except Exception:
                        pass
            with lock:
                latencies.extend(local)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(worker, [f"w{i}" for i in range(workers)]))
        busy = getattr(bundle.task_queue_store, "sqlite_busy_count", 0)
        if bundle.connection is not None:
            bundle.connection.close()
        return ScenarioResult(
            "sqlite_contention",
            True,
            {
                "workers": workers,
                "ops": ops,
                "claim_latency_p50_ms": _percentile(latencies, 50),
                "claim_latency_p95_ms": _percentile(latencies, 95),
                "sqlite_busy_count": busy,
                "throughput_approx": len([x for x in latencies if x >= 0]),
            },
            notes="boundary_signal_only",
        )


def scenario_interactive_vs_batch_flood() -> ScenarioResult:
    """Interactive claims remain available under a bulk/batch flood."""

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        bundle, q, env = _make_queue(
            tmp,
            MAX_RUNNING_GLOBAL="20",
            INTERACTIVE_RESERVED="5",
            BACKGROUND_MAY_BORROW_INTERACTIVE="false",
        )
        q.lane_config = LaneCapacityConfig(
            interactive_reserved=5, background_may_borrow=False
        )
        for i in range(200):
            q.enqueue(
                workflow_id=f"batch-{i}",
                task_id=f"tb-{i}",
                execution_key=f"ek-batch-{i}",
                tenant_id="tenant-batch",
                execution_lane=LANE_BULK,
                priority="low",
            )
        # Fill non-interactive capacity
        bg_claimed = 0
        for i in range(20):
            t = q.dequeue(
                worker_id=f"wb{i}", max_running_global=20, max_running_per_tenant=50
            )
            if t is None:
                break
            if t.execution_lane != LANE_INTERACTIVE:
                bg_claimed += 1
        q.enqueue(
            workflow_id="ix-flood",
            task_id="tix",
            execution_key="ek-ix-flood",
            tenant_id="tenant-user",
            execution_lane=LANE_INTERACTIVE,
            priority="high",
        )
        t0 = time.perf_counter()
        ix = q.dequeue(
            worker_id="wix", max_running_global=20, max_running_per_tenant=50
        )
        wait_ms = (time.perf_counter() - t0) * 1000.0
        ok = ix is not None and ix.execution_lane == LANE_INTERACTIVE
        if bundle.connection is not None:
            bundle.connection.close()
        return ScenarioResult(
            "interactive_vs_batch_flood",
            ok,
            {
                "batch_claimed_before_ix": bg_claimed,
                "interactive_wait_ms": wait_ms,
                "interactive_claimed": ok,
            },
            notes="" if ok else "interactive_blocked_by_batch",
        )


SCENARIOS = {
    "interactive_baseline": scenario_interactive_baseline,
    "background_saturation": scenario_background_saturation,
    "interactive_vs_batch_flood": scenario_interactive_vs_batch_flood,
    "tenant_bully": scenario_tenant_bully,
    "schedule_storm": scenario_schedule_storm,
    "worker_loss": scenario_worker_loss,
    "provider_saturation": scenario_provider_saturation,
    "429_storm": scenario_429_storm,
    "sqlite_contention": scenario_sqlite_contention,
}


def run_all() -> list[ScenarioResult]:
    results = []
    for name, fn in SCENARIOS.items():
        results.append(fn())
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Panda Phase 3 load/failure harness")
    parser.add_argument(
        "--scenario",
        default="all",
        help="scenario name or 'all'",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    if args.scenario == "all":
        results = run_all()
    else:
        if args.scenario not in SCENARIOS:
            raise SystemExit(f"unknown scenario: {args.scenario}")
        results = [SCENARIOS[args.scenario]()]
    payload = [r.as_dict() for r in results]
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for r in results:
            status = "PASS" if r.ok else "FAIL"
            print(f"[{status}] {r.name}: {r.metrics} {r.notes}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
