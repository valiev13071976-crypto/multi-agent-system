"""PATCH-MR-05 — multi-worker routing health isolation contract."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from agents.model_profile import build_model_profile
from agents.model_router import ModelRouter
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.routing_health import (
    HEALTH_COOLDOWN,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    ProviderHealthTracker,
    RoutingHealthPolicy,
)
from agents.routing_runtime_stats import (
    DEFAULT_RUNTIME_TIEBREAK_ENABLED,
    ProviderRuntimeStatsAggregator,
    RuntimeStatsPolicy,
    load_runtime_stats_policy,
)
from agents.routing_state_scope import (
    SHARED_BACKING_NOT_AVAILABLE,
    STATE_SCOPE_PROCESS_LOCAL,
    ProviderHealthStore,
    ProviderRuntimeStatsStore,
    routing_coordination_capabilities,
)
from config.runtime_health import (
    STATUS_HEALTHY,
    STATUS_NOT_READY,
    evaluate_readiness,
)
from observability.runtime_metrics import collect_operational_metrics
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets


def _profile(provider_id, model, *, coding=True, quality="standard"):
    return build_model_profile(
        provider_id,
        model,
        task_categories_raw="general,technical",
        coding_raw="true" if coding else "false",
        quality_raw=quality,
        cost_raw="standard",
    )


def _registry():
    profiles = {
        "openai": _profile("openai", "premium", quality="premium"),
        "anthropic": _profile("anthropic", "cheap", quality="standard"),
    }
    records = {
        "openai": ProviderRecord("openai", "premium", True),
        "anthropic": ProviderRecord("anthropic", "cheap", True),
    }
    for pid in ("gemini", "grok", "deepseek", "moonshot", "mistral"):
        records[pid] = ProviderRecord(pid, f"{pid}-m", False)
        profiles[pid] = _profile(pid, f"{pid}-m")
    return ProviderRegistry(
        records,
        profiles=profiles,
        auto_provider_order=("openai", "anthropic"),
        auto_routing_policy="quality",
        auto_capability_fallback="error",
    )


class HealthTrackerIsolationTests(unittest.TestCase):
    def test_case1_independent_trackers_do_not_share_cooldown(self):
        policy = RoutingHealthPolicy(failure_threshold=2, cooldown_seconds=600)
        tracker_a = ProviderHealthTracker(policy)
        tracker_b = ProviderHealthTracker(policy)
        self.assertIsNot(tracker_a, tracker_b)
        self.assertIsNot(tracker_a._events, tracker_b._events)

        tracker_a.record_failure("openai", "premium", error_class="TimeoutError")
        tracker_a.record_failure("openai", "premium", error_class="TimeoutError")
        self.assertEqual(tracker_a.snapshot("openai", "premium").state, HEALTH_COOLDOWN)
        self.assertFalse(tracker_a.is_auto_eligible("openai", "premium"))

        snap_b = tracker_b.snapshot("openai", "premium")
        self.assertEqual(snap_b.state, HEALTH_UNKNOWN)
        self.assertTrue(tracker_b.is_auto_eligible("openai", "premium"))

    def test_case2_recovery_remains_local(self):
        policy = RoutingHealthPolicy(failure_threshold=2, cooldown_seconds=600)
        tracker_a = ProviderHealthTracker(policy)
        tracker_b = ProviderHealthTracker(policy)
        tracker_a.record_failure("openai", "premium", error_class="TimeoutError")
        tracker_a.record_failure("openai", "premium", error_class="TimeoutError")
        tracker_b.record_failure("openai", "premium", error_class="TimeoutError")
        tracker_b.record_failure("openai", "premium", error_class="TimeoutError")
        self.assertEqual(tracker_a.snapshot("openai", "premium").state, HEALTH_COOLDOWN)
        self.assertEqual(tracker_b.snapshot("openai", "premium").state, HEALTH_COOLDOWN)

        tracker_a.record_success("openai", "premium")
        self.assertEqual(tracker_a.snapshot("openai", "premium").state, HEALTH_HEALTHY)
        self.assertEqual(tracker_b.snapshot("openai", "premium").state, HEALTH_COOLDOWN)


class RouterIsolationTests(unittest.TestCase):
    def test_case3_router_instances_isolate_health(self):
        policy = RoutingHealthPolicy(failure_threshold=2, cooldown_seconds=600)
        reg = _registry()
        tracker_a = ProviderHealthTracker(policy)
        tracker_b = ProviderHealthTracker(policy)
        router_a = ModelRouter(reg, health_tracker=tracker_a)
        router_b = ModelRouter(reg, health_tracker=tracker_b)

        tracker_a.record_failure("openai", "premium", error_class="TimeoutError")
        tracker_a.record_failure("openai", "premium", error_class="TimeoutError")

        decision_a = router_a.decide("auto", "technical", category="technical")
        decision_b = router_b.decide("auto", "technical", category="technical")
        self.assertEqual(decision_a.provider_ids, ("anthropic",))
        self.assertEqual(decision_b.provider_ids, ("openai",))


class RuntimeStatsIsolationTests(unittest.TestCase):
    def test_case4_runtime_stats_instances_isolated(self):
        policy = RuntimeStatsPolicy(min_samples=1, window_seconds=900)
        stats_a = ProviderRuntimeStatsAggregator(policy)
        stats_b = ProviderRuntimeStatsAggregator(policy)
        self.assertIsNot(stats_a._samples, stats_b._samples)

        stats_a.record_success(
            "openai",
            "premium",
            latency_ms=12.0,
            cost=Decimal("0.01"),
        )
        snap_a = stats_a.snapshot("openai", "premium")
        snap_b = stats_b.snapshot("openai", "premium")
        self.assertGreaterEqual(snap_a.sample_count, 1)
        self.assertEqual(snap_b.sample_count, 0)

    def test_case5_tiebreak_remains_off_by_default(self):
        self.assertFalse(DEFAULT_RUNTIME_TIEBREAK_ENABLED)
        policy = load_runtime_stats_policy(env={})
        self.assertFalse(policy.tiebreak_enabled)
        caps = routing_coordination_capabilities()
        self.assertFalse(caps["runtime_stats_tiebreak_enabled_default"])


class OperationalCapabilityTests(unittest.TestCase):
    def test_case6_capability_signal_process_local(self):
        tracker = ProviderHealthTracker()
        stats = ProviderRuntimeStatsAggregator()
        self.assertEqual(tracker.state_scope, STATE_SCOPE_PROCESS_LOCAL)
        self.assertFalse(tracker.shared_backing)
        self.assertEqual(stats.state_scope, STATE_SCOPE_PROCESS_LOCAL)
        self.assertFalse(stats.shared_backing)
        self.assertIsInstance(tracker, ProviderHealthStore)
        self.assertIsInstance(stats, ProviderRuntimeStatsStore)

        caps = routing_coordination_capabilities(
            health_tracker=tracker, runtime_stats=stats
        )
        self.assertEqual(caps["routing_health_scope"], STATE_SCOPE_PROCESS_LOCAL)
        self.assertEqual(caps["routing_runtime_stats_scope"], STATE_SCOPE_PROCESS_LOCAL)
        self.assertEqual(
            caps["routing_health_shared_backing"], SHARED_BACKING_NOT_AVAILABLE
        )
        self.assertEqual(
            caps["routing_runtime_stats_shared_backing"], SHARED_BACKING_NOT_AVAILABLE
        )
        self.assertFalse(caps["multi_worker_shared_routing_health_ready"])

        snap = evaluate_readiness(
            health_tracker=tracker,
            runtime_stats=stats,
            env={"RUNTIME_ROLE": "api"},
            draining=False,
        )
        self.assertEqual(snap.liveness, STATUS_HEALTHY)
        self.assertEqual(
            snap.capabilities["routing_health_scope"], STATE_SCOPE_PROCESS_LOCAL
        )
        names = {d.name: d for d in snap.dependencies}
        self.assertIn("routing_provider_health", names)
        self.assertIn("process_local", names["routing_provider_health"].detail)
        self.assertIn("not_available", names["routing_provider_health"].detail)
        self.assertEqual(names["routing_provider_health"].status, STATUS_HEALTHY)

        metrics = collect_operational_metrics(
            health_tracker=tracker, runtime_stats=stats
        )
        self.assertEqual(
            metrics["routing_coordination"]["routing_health_scope"],
            STATE_SCOPE_PROCESS_LOCAL,
        )

    def test_case7_single_process_readiness_not_failed_for_process_local(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "ready.sqlite3")
            env = {
                "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                "SIDE_EFFECT_DB_PATH": path,
                "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                "RUNTIME_ROLE": "api",
                "INTEGRATION_SECRETS_BACKEND": "memory",
                "PANDA_RUNTIME_PROFILE": "development",
            }
            runtime = compose_side_effect_runtime(secrets=DictSecrets(), env=env)
            try:
                snap = evaluate_readiness(
                    side_effect_runtime=runtime,
                    env=env,
                    health_tracker=ProviderHealthTracker(),
                    runtime_stats=ProviderRuntimeStatsAggregator(),
                )
                self.assertEqual(snap.liveness, STATUS_HEALTHY)
                self.assertNotEqual(snap.readiness, STATUS_NOT_READY)
                self.assertEqual(
                    snap.capabilities["routing_health_shared_backing"],
                    SHARED_BACKING_NOT_AVAILABLE,
                )
                self.assertFalse(
                    snap.capabilities["multi_worker_shared_routing_health_ready"]
                )
            finally:
                runtime.close()


class RoutingBehaviorRegressionTests(unittest.TestCase):
    def test_case8_cooldown_exclusion_and_recovery_unchanged(self):
        policy = RoutingHealthPolicy(failure_threshold=2, cooldown_seconds=600)
        tracker = ProviderHealthTracker(policy)
        router = ModelRouter(_registry(), health_tracker=tracker)

        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        self.assertEqual(tracker.snapshot("openai", "premium").state, HEALTH_COOLDOWN)
        decision = router.decide("auto", "technical", category="technical")
        self.assertEqual(decision.provider_ids, ("anthropic",))

        tracker.record_success("openai", "premium")
        self.assertEqual(tracker.snapshot("openai", "premium").state, HEALTH_HEALTHY)
        recovered = router.decide("auto", "technical", category="technical")
        self.assertEqual(recovered.provider_ids, ("openai",))

        # Cooldown expiry still clears with wall-clock advance (local state only).
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expire_tracker = ProviderHealthTracker(
            RoutingHealthPolicy(failure_threshold=2, cooldown_seconds=30)
        )
        expire_tracker.record_failure(
            "openai", "premium", error_class="TimeoutError", now=t0
        )
        expire_tracker.record_failure(
            "openai",
            "premium",
            error_class="TimeoutError",
            now=t0 + timedelta(seconds=1),
        )
        self.assertEqual(
            expire_tracker.snapshot(
                "openai", "premium", now=t0 + timedelta(seconds=2)
            ).state,
            HEALTH_COOLDOWN,
        )
        after = expire_tracker.snapshot(
            "openai", "premium", now=t0 + timedelta(seconds=40)
        )
        self.assertNotEqual(after.state, HEALTH_COOLDOWN)
        self.assertTrue(after.auto_eligible)


if __name__ == "__main__":
    unittest.main()
