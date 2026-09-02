"""Closure tests — Scale / Optimization Based on Real Metrics."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from production_business_e2e.fixtures import auth_env, api_headers
from scale_optimization.access import ScaleOptimizationAccessPolicy
from scale_optimization.autoscaling import AutoscalingSignalEngine
from scale_optimization.benchmark import PROFILES, run_all_profiles, run_profile
from scale_optimization.bottleneck import detect_bottleneck, recommend_scale
from scale_optimization.capacity import evaluate_capacity
from scale_optimization.config import (
    scale_optimization_engineering_ready,
    scale_optimization_live_active,
    scale_optimization_live_verified,
)
from scale_optimization.errors import FORBIDDEN, INVALID_COMPARISON, INVALID_LABEL, ScaleOptimizationError
from scale_optimization.evidence import compare_before_after
from scale_optimization.metrics import MetricRegistry, aggregate_percentiles, percentile, redact_for_export, validate_labels
from scale_optimization.models import (
    BN_PROVIDER,
    BN_QUEUE,
    BN_UNKNOWN,
    BN_WORKER_POOL,
    CAP_HEALTHY,
    CAP_NEAR_CAPACITY,
    CAP_OVERLOADED,
    CAP_SATURATED,
    CAP_INSUFFICIENT_DATA,
    REC_BATCH_DEFER,
    REC_NO_ACTION,
    REC_SCALE_OUT,
    REC_SHED_LOAD,
    SLO_BREACHED,
    SLO_HEALTHY,
    SLO_INSUFFICIENT_DATA,
    SLO_WARNING,
)
from scale_optimization.router import configure_scale_optimization_router
from scale_optimization.runtime import build_scale_optimization_runtime
from scale_optimization.service import ScaleOptimizationService
from scale_optimization.slo import evaluate_slo
from security.api_auth import configure_security
from security.identity import RequestSecurityContext


def _ctx(tenant: str = "tenant-a", roles=("admin",)) -> RequestSecurityContext:
    return RequestSecurityContext(tenant_id=tenant, user_id="u1", roles=roles, request_id="r1")


class FlagTests(unittest.TestCase):
    def test_flags(self):
        self.assertTrue(scale_optimization_engineering_ready())
        self.assertFalse(scale_optimization_live_active())
        self.assertFalse(scale_optimization_live_verified())


class MetricTests(unittest.TestCase):
    def test_percentile_and_aggregation(self):
        vals = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        self.assertEqual(percentile(vals, 50), 55.0)
        stats = aggregate_percentiles(vals)
        self.assertEqual(stats.count, 10)
        self.assertGreaterEqual(stats.p95, stats.p50)
        self.assertGreaterEqual(stats.p99, stats.p95)

    def test_workload_separation(self):
        reg = MetricRegistry()
        reg.emit("request_latency_ms", 10, labels={"workload_class": "INTERACTIVE"})
        reg.emit("request_latency_ms", 500, labels={"workload_class": "BATCH"})
        self.assertEqual(reg.stats("request_latency_ms", labels={"workload_class": "INTERACTIVE"}).avg, 10)
        self.assertEqual(reg.stats("request_latency_ms", labels={"workload_class": "BATCH"}).avg, 500)

    def test_forbidden_labels(self):
        with self.assertRaises(ScaleOptimizationError) as cm:
            validate_labels({"prompt": "secret text"})
        self.assertEqual(cm.exception.code, INVALID_LABEL)

    def test_bounded_cardinality(self):
        with self.assertRaises(ScaleOptimizationError):
            validate_labels({f"operation_{i}": "x" for i in range(100)})

    def test_secret_redaction(self):
        out = redact_for_export({"api_key": "sk-live", "workload_class": "INTERACTIVE"})
        self.assertEqual(out["api_key"], "[REDACTED]")
        self.assertEqual(out["workload_class"], "INTERACTIVE")


class SLOTests(unittest.TestCase):
    def test_insufficient_data(self):
        r = evaluate_slo(metric="lat", target=100, observed=50, sample_count=1, window_seconds=60, workload_class="INTERACTIVE")
        self.assertEqual(r.status, SLO_INSUFFICIENT_DATA)

    def test_healthy(self):
        r = evaluate_slo(metric="lat", target=100, observed=50, sample_count=20, window_seconds=60, workload_class="INTERACTIVE")
        self.assertEqual(r.status, SLO_HEALTHY)

    def test_warning(self):
        r = evaluate_slo(metric="lat", target=100, observed=105, sample_count=20, window_seconds=60, workload_class="INTERACTIVE", warning_ratio=0.9)
        self.assertEqual(r.status, SLO_WARNING)

    def test_breached(self):
        r = evaluate_slo(metric="lat", target=100, observed=500, sample_count=20, window_seconds=60, workload_class="INTERACTIVE")
        self.assertEqual(r.status, SLO_BREACHED)


class CapacityTests(unittest.TestCase):
    def test_insufficient(self):
        c = evaluate_capacity(sample_count=0)
        self.assertEqual(c.state, CAP_INSUFFICIENT_DATA)

    def test_healthy(self):
        c = evaluate_capacity(sample_count=10, active_concurrency=2, max_concurrency=10, queue_depth=1, arrival_rate=1, completion_rate=1)
        self.assertEqual(c.state, CAP_HEALTHY)

    def test_near(self):
        c = evaluate_capacity(sample_count=10, active_concurrency=9, max_concurrency=10, queue_depth=5, arrival_rate=1, completion_rate=1)
        self.assertEqual(c.state, CAP_NEAR_CAPACITY)

    def test_saturated(self):
        c = evaluate_capacity(sample_count=10, active_concurrency=10, max_concurrency=10, queue_depth=5, arrival_rate=1, completion_rate=1)
        self.assertEqual(c.state, CAP_SATURATED)

    def test_overloaded(self):
        c = evaluate_capacity(sample_count=10, active_concurrency=10, max_concurrency=10, queue_depth=20, arrival_rate=5, completion_rate=1, rejection_count=3)
        self.assertEqual(c.state, CAP_OVERLOADED)


class BottleneckTests(unittest.TestCase):
    def test_queue(self):
        b = detect_bottleneck(queue_depth=200, sample_count=20)
        self.assertEqual(b.category, BN_QUEUE)

    def test_worker(self):
        b = detect_bottleneck(worker_utilization=0.99, sample_count=20)
        self.assertEqual(b.category, BN_WORKER_POOL)

    def test_provider(self):
        b = detect_bottleneck(provider_429_rate=0.2, sample_count=20)
        self.assertEqual(b.category, BN_PROVIDER)

    def test_unknown(self):
        b = detect_bottleneck(sample_count=0)
        self.assertEqual(b.category, BN_UNKNOWN)

    def test_load_shed_recommendation(self):
        b = detect_bottleneck(queue_depth=600, sample_count=20)
        rec = recommend_scale(b)
        self.assertIn(rec.action, {REC_SCALE_OUT, REC_SHED_LOAD, "SCALE_OUT"})

    def test_interactive_batch_defer(self):
        b = detect_bottleneck(queue_depth=200, sample_count=20)
        rec = recommend_scale(b, interactive_pressure=True, batch_pressure=True)
        self.assertEqual(rec.action, REC_BATCH_DEFER)


class AutoscalingTests(unittest.TestCase):
    def test_insufficient(self):
        eng = AutoscalingSignalEngine(cooldown_seconds=1, min_window_seconds=30)
        r = eng.evaluate(
            queue_depth=10, oldest_age_seconds=1, arrival_completion_delta=0, worker_utilization=0.1,
            interactive_latency_p95_ms=10, provider_saturation=0.1, sample_count=1, observation_window_seconds=1,
        )
        self.assertEqual(r.action, "INSUFFICIENT_DATA")

    def test_scale_signal(self):
        eng = AutoscalingSignalEngine(cooldown_seconds=0, min_window_seconds=1)
        r = eng.evaluate(
            queue_depth=80, oldest_age_seconds=90, arrival_completion_delta=5, worker_utilization=0.9,
            interactive_latency_p95_ms=2500, provider_saturation=0.2, sample_count=20, observation_window_seconds=60,
        )
        self.assertIn(r.action, {REC_SCALE_OUT, "INCREASE_POOL"})

    def test_cooldown_and_hysteresis_no_flapping(self):
        eng = AutoscalingSignalEngine(cooldown_seconds=3600, hysteresis_ratio=0.15, min_window_seconds=1)
        now = datetime.now(timezone.utc)
        r1 = eng.evaluate(
            queue_depth=80, oldest_age_seconds=90, arrival_completion_delta=5, worker_utilization=0.9,
            interactive_latency_p95_ms=2500, provider_saturation=0.2, sample_count=20, observation_window_seconds=60,
            now_iso=now.isoformat(),
        )
        self.assertNotEqual(r1.action, REC_NO_ACTION)
        r2 = eng.evaluate(
            queue_depth=0, oldest_age_seconds=0, arrival_completion_delta=-1, worker_utilization=0.1,
            interactive_latency_p95_ms=10, provider_saturation=0.1, sample_count=20, observation_window_seconds=60,
            now_iso=(now + timedelta(seconds=1)).isoformat(),
        )
        self.assertEqual(r2.action, REC_NO_ACTION)
        self.assertFalse(r2.cooldown_ok)


class FairnessRetryTests(unittest.TestCase):
    def test_noisy_tenant(self):
        b = detect_bottleneck(tenant_share=0.85, sample_count=20)
        self.assertEqual(b.category, "TENANT_HOTSPOT")

    def test_retry_amplification(self):
        b = detect_bottleneck(retry_ratio=0.8, sample_count=20)
        self.assertEqual(b.category, "RETRY_AMPLIFICATION")
        result = run_profile("G_retry_pressure", sample_count=20)
        self.assertGreater(result.cost["retry_attempts"], 0)


class BenchmarkTests(unittest.TestCase):
    def test_all_profiles_machine_readable(self):
        results = run_all_profiles(sample_count=10)
        self.assertEqual(len(results), len(PROFILES))
        for r in results:
            d = r.as_dict()
            self.assertIn("latency", d)
            self.assertIn("verdict", d)
            self.assertFalse(d["live"])
            self.assertEqual(d["mode"], "FIXTURE")

    def test_deterministic(self):
        a = run_profile("A_interactive_light", sample_count=15).latency
        b = run_profile("A_interactive_light", sample_count=15).latency
        self.assertEqual(a, b)

    def test_excel_and_crawler(self):
        j = run_profile("J_large_excel")
        k = run_profile("K_crawler_batch")
        self.assertGreater(j.capacity_signals["input_rows"], 0)
        self.assertGreater(k.capacity_signals["pages"], 0)

    def test_persistence_and_overload(self):
        self.assertEqual(run_profile("L_persistence_pressure").latency_breakdown["persistence_time_ms"], 80.0)
        self.assertEqual(run_profile("I_queue_overload").verdict, "DEGRADED")

    def test_provider_profiles(self):
        self.assertTrue(run_profile("F_provider_429").capacity_signals["provider_saturation"] >= 0.9)
        self.assertGreater(run_profile("E_provider_slowdown").latency["p95"], 100)


class EvidenceTests(unittest.TestCase):
    def test_comparable(self):
        before = {"profile": "A", "workload_class": "INTERACTIVE", "latency": {"p95": 100}}
        after = {"profile": "A", "workload_class": "INTERACTIVE", "latency": {"p95": 50}}
        ev = compare_before_after(path="x", before=before, after=after, change="noop", workload_profile="A")
        self.assertTrue(ev.improvement)

    def test_invalid_comparison(self):
        with self.assertRaises(ScaleOptimizationError) as cm:
            compare_before_after(
                path="x",
                before={"profile": "A", "workload_class": "INTERACTIVE", "latency": {"p95": 1}},
                after={"profile": "B", "workload_class": "INTERACTIVE", "latency": {"p95": 1}},
                change="x",
                workload_profile="A",
            )
        self.assertEqual(cm.exception.code, INVALID_COMPARISON)


class ServiceAPITests(unittest.TestCase):
    def test_analyze_and_view(self):
        svc = ScaleOptimizationService()
        out = svc.analyze(_ctx(), signals={
            "sample_count": 20,
            "queue_depth": 5,
            "active_concurrency": 2,
            "max_concurrency": 10,
            "arrival_rate": 1,
            "completion_rate": 1,
            "observation_window_seconds": 60,
            "worker_utilization": 0.2,
        })
        self.assertIn("capacity", out)
        self.assertFalse(out["live"])
        view = svc.management_view(_ctx(), tenant_id="tenant-a")
        self.assertIn("recommendations", view)

    def test_rbac_viewer_cannot_benchmark(self):
        svc = ScaleOptimizationService()
        with self.assertRaises(ScaleOptimizationError) as cm:
            svc.run_benchmark(_ctx(roles=("viewer",)))
        self.assertEqual(cm.exception.code, FORBIDDEN)

    def test_tenant_isolation(self):
        policy = ScaleOptimizationAccessPolicy()
        with self.assertRaises(ScaleOptimizationError):
            policy.require(_ctx("tenant-a"), "scale.read", tenant_id="tenant-b")

    def test_http_status(self):
        for k, v in auth_env().items():
            os.environ[k] = v
        os.environ["PANDA_API_KEYS"] = (
            "key-a|tenant-a|user-a|admin,operator|secret-a;"
            "key-b|tenant-b|user-b|user|secret-b"
        )
        configure_security()
        rt = build_scale_optimization_runtime()
        app = FastAPI()
        app.include_router(configure_scale_optimization_router(rt.service, rt.policy))
        client = TestClient(app)
        st = client.get("/api/v1/scale/optimization/status")
        self.assertEqual(st.status_code, 200)
        self.assertTrue(st.json()["engineering_ready"])
        self.assertFalse(st.json()["live_active"])
        bench = client.post("/api/v1/scale/optimization/benchmark?profile=A_interactive_light", headers=api_headers())
        self.assertEqual(bench.status_code, 200)
        self.assertFalse(bench.json()["live"])

    def test_no_live_mutation(self):
        svc = ScaleOptimizationService()
        out = svc.run_benchmark(_ctx(), profile="A_interactive_light")
        self.assertFalse(out["infrastructure_mutation"])
        self.assertFalse(out["live"])


class CostAttributionTests(unittest.TestCase):
    def test_cost_and_duplicate_retry(self):
        r = run_profile("G_retry_pressure", sample_count=20)
        self.assertIn("cost_units", r.cost)
        self.assertGreaterEqual(r.cost["original_requests"] + r.cost["retry_attempts"], r.cost["original_requests"])


class PreservationTests(unittest.TestCase):
    def test_imports_existing_platform(self):
        from runtime.capacity_snapshot import CapacitySnapshot, build_capacity_snapshot
        from task_queue.lanes import LANE_INTERACTIVE
        from providers.governor import ProviderGovernor
        self.assertTrue(callable(build_capacity_snapshot))
        self.assertEqual(LANE_INTERACTIVE, "interactive")
        self.assertTrue(ProviderGovernor is not None)


if __name__ == "__main__":
    unittest.main()
