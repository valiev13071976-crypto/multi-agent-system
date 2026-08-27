"""P1.2 runtime Quality/Latency/Cost stats — offline only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import asyncio
import unittest

from agents.core.expert_manager import ExpertManager, FinOpsBudgetDeniedError
from agents.model_profile import build_model_profile
from agents.model_router import (
    REASON_AUTO_CAPABILITY_MATCH,
    REASON_EXPLICIT_PROVIDER,
    ModelRouter,
    NoCapableProviderError,
    ProviderCapabilityMismatchError,
)
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.provider_result import ProviderResult
from agents.routing_health import ProviderHealthTracker, RoutingHealthPolicy
from agents.routing_requirements import CAPABILITY_CODING, TaskRequirements
from agents.routing_runtime_stats import (
    STATS_INSUFFICIENT,
    STATS_READY,
    STATS_UNKNOWN,
    ProviderRuntimeStatsAggregator,
    RuntimeStatsPolicy,
)
from finops.models import BudgetLimits, PriceQuote
from finops.service import FinOpsService


def _profile(provider_id, model, *, coding=True, categories="general,technical", quality="standard"):
    return build_model_profile(
        provider_id,
        model,
        task_categories_raw=categories,
        coding_raw="true" if coding else "false",
        quality_raw=quality,
        cost_raw="standard",
        latency_raw="standard",
    )


def _registry(*, policy="priority"):
    profiles = {
        "openai": _profile("openai", "premium", quality="premium"),
        "anthropic": _profile("anthropic", "cheap", quality="premium"),
    }
    records = {
        "openai": ProviderRecord("openai", "premium", True),
        "anthropic": ProviderRecord("anthropic", "cheap", True),
    }
    for pid in ("gemini", "grok", "deepseek", "moonshot", "mistral"):
        records[pid] = ProviderRecord(pid, f"{pid}-m", False)
        profiles[pid] = _profile(pid, f"{pid}-m", categories="general")
    return ProviderRegistry(
        records,
        profiles=profiles,
        auto_provider_order=("openai", "anthropic"),
        auto_routing_policy=policy,
        auto_capability_fallback="error",
    )


class RuntimeStatsUnitTests(unittest.TestCase):
    def test_success_contributes_latency(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, window_seconds=300)
        )
        snap = agg.record_success("openai", "premium", latency_ms=120.0, cost=Decimal("0.01"))
        self.assertEqual(snap.success_count, 1)
        self.assertEqual(snap.latency_avg_ms, 120.0)
        self.assertEqual(snap.cost_avg, Decimal("0.01"))
        self.assertEqual(snap.state, STATS_READY)

    def test_failure_contributes_failure(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, window_seconds=300)
        )
        snap = agg.record_failure("openai", "premium", latency_ms=50.0)
        self.assertEqual(snap.failure_count, 1)
        self.assertEqual(snap.success_count, 0)
        self.assertEqual(snap.success_rate, 0.0)

    def test_unknown_cost_remains_unknown(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, window_seconds=300)
        )
        snap = agg.record_success("openai", "premium", latency_ms=10.0, cost=None)
        self.assertIsNone(snap.cost_avg)

    def test_bounded_window_expires(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, window_seconds=30)
        )
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        agg.record_success("openai", "premium", latency_ms=10.0, now=t0)
        snap = agg.snapshot("openai", "premium", now=t0 + timedelta(seconds=60))
        self.assertEqual(snap.state, STATS_UNKNOWN)
        self.assertEqual(snap.sample_count, 0)

    def test_below_min_samples_insufficient(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=5, window_seconds=300)
        )
        for _ in range(3):
            agg.record_success("openai", "premium", latency_ms=10.0)
        snap = agg.snapshot("openai", "premium")
        self.assertEqual(snap.state, STATS_INSUFFICIENT)
        self.assertFalse(snap.usable)

    def test_sufficient_samples_ready(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=3, window_seconds=300)
        )
        for i in range(3):
            agg.record_success("openai", "premium", latency_ms=10.0 + i)
        snap = agg.snapshot("openai", "premium")
        self.assertEqual(snap.state, STATS_READY)
        self.assertTrue(snap.usable)
        self.assertAlmostEqual(snap.latency_avg_ms, 11.0)


class RuntimeStatsIntegrationTests(unittest.TestCase):
    def test_expert_manager_records_success_and_cost(self):
        finops = FinOpsService(
            prices={
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("1000"), Decimal("1000"), "USD", True
                ),
            },
            limits=BudgetLimits(None, None, None, "allow"),
        )
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, window_seconds=300)
        )

        class Agent:
            model = "premium"

            async def run(self, prompt):
                return ProviderResult(
                    text="ok",
                    provider_id="openai",
                    model_id="premium",
                    input_tokens=1000,
                    output_tokens=500,
                    total_tokens=1500,
                )

        manager = ExpertManager(openai=Agent(), finops=finops)
        manager.runtime_stats = agg
        asyncio.run(manager.run("hi", selected=[("openai", Agent())], task_id="t1"))
        snap = agg.snapshot("openai", "premium")
        self.assertEqual(snap.success_count, 1)
        self.assertIsNotNone(snap.latency_avg_ms)
        self.assertIsNotNone(snap.cost_avg)
        self.assertGreater(snap.cost_avg, Decimal("0"))

    def test_budget_denial_does_not_pollute_stats(self):
        finops = FinOpsService(
            prices={},
            limits=BudgetLimits(None, None, None, "deny"),
        )
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, window_seconds=300)
        )

        class Agent:
            model = "premium"

            async def run(self, prompt):
                return "ok"

        manager = ExpertManager(openai=Agent(), finops=finops)
        manager.runtime_stats = agg
        with self.assertRaises(FinOpsBudgetDeniedError):
            asyncio.run(manager.run("hi", selected=[("openai", Agent())], task_id="t2"))
        self.assertEqual(agg.snapshot("openai", "premium").state, STATS_UNKNOWN)

    def test_capability_mismatch_does_not_record(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, window_seconds=300)
        )
        reg = _registry()
        profiles = dict(reg._profiles)
        profiles["openai"] = _profile("openai", "premium", coding=False)
        reg = ProviderRegistry(
            reg._records,
            profiles=profiles,
            auto_provider_order=("openai", "anthropic"),
            auto_routing_policy="priority",
            auto_capability_fallback="error",
        )
        with self.assertRaises(ProviderCapabilityMismatchError):
            ModelRouter(reg, runtime_stats=agg).decide(
                "openai",
                "technical",
                category="technical",
                requirements=TaskRequirements(required_capabilities=(CAPABILITY_CODING,)),
            )
        self.assertEqual(agg.snapshot("openai", "premium").state, STATS_UNKNOWN)

    def test_health_independent_of_runtime_stats(self):
        health = ProviderHealthTracker(RoutingHealthPolicy(failure_threshold=2))
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, window_seconds=300)
        )
        health.record_failure("openai", "premium", error_class="TimeoutError")
        health.record_failure("openai", "premium", error_class="TimeoutError")
        agg.record_success("openai", "premium", latency_ms=10.0)
        decision = ModelRouter(
            _registry(), health_tracker=health, runtime_stats=agg
        ).decide("auto", "technical", category="technical")
        self.assertEqual(decision.provider_ids, ("anthropic",))

    def test_capability_still_authoritative(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, tiebreak_enabled=True)
        )
        for _ in range(5):
            agg.record_success("anthropic", "cheap", latency_ms=5.0)
        with self.assertRaises(NoCapableProviderError):
            ModelRouter(
                _registry().__class__(
                    {
                        "openai": ProviderRecord("openai", "premium", True),
                        "anthropic": ProviderRecord("anthropic", "cheap", True),
                        "gemini": ProviderRecord("gemini", "g", False),
                        "grok": ProviderRecord("grok", "g", False),
                        "deepseek": ProviderRecord("deepseek", "d", False),
                        "moonshot": ProviderRecord("moonshot", "m", False),
                        "mistral": ProviderRecord("mistral", "m", False),
                    },
                    profiles={
                        "openai": _profile("openai", "premium", coding=False),
                        "anthropic": _profile("anthropic", "cheap", coding=False),
                        "gemini": _profile("gemini", "g"),
                        "grok": _profile("grok", "g"),
                        "deepseek": _profile("deepseek", "d"),
                        "moonshot": _profile("moonshot", "m"),
                        "mistral": _profile("mistral", "m"),
                    },
                    auto_provider_order=("openai", "anthropic"),
                    auto_routing_policy="priority",
                    auto_capability_fallback="error",
                ),
                runtime_stats=agg,
            ).decide(
                "auto",
                "technical",
                category="technical",
                requirements=TaskRequirements(required_capabilities=(CAPABILITY_CODING,)),
            )

    def test_explicit_never_changes(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, tiebreak_enabled=True)
        )
        for _ in range(5):
            agg.record_success("anthropic", "cheap", latency_ms=1.0, cost=Decimal("0.001"))
        decision = ModelRouter(_registry(), runtime_stats=agg).decide(
            "openai", "technical", category="technical"
        )
        self.assertEqual(decision.provider_ids, ("openai",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)

    def test_default_routing_same_as_p11_when_tiebreak_disabled(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, tiebreak_enabled=False)
        )
        for _ in range(5):
            agg.record_success(
                "anthropic", "cheap", latency_ms=1.0, cost=Decimal("0.001")
            )
        reg = _registry(policy="priority")
        without = ModelRouter(reg).decide("auto", "technical", category="technical")
        with_stats = ModelRouter(reg, runtime_stats=agg).decide(
            "auto", "technical", category="technical"
        )
        self.assertEqual(without.provider_ids, with_stats.provider_ids)
        self.assertEqual(without.provider_ids, ("openai",))
        self.assertEqual(with_stats.reason, REASON_AUTO_CAPABILITY_MATCH)

    def test_audit_contains_runtime_factors(self):
        agg = ProviderRuntimeStatsAggregator(
            RuntimeStatsPolicy(min_samples=1, tiebreak_enabled=False)
        )
        agg.record_success("openai", "premium", latency_ms=42.0, cost=Decimal("0.02"))
        decision = ModelRouter(_registry(), runtime_stats=agg).decide(
            "auto", "technical", category="technical"
        )
        snap = decision.factor_snapshot
        self.assertEqual(snap.runtime_stats_state, STATS_READY)
        self.assertEqual(snap.runtime_sample_count, 1)
        self.assertEqual(snap.runtime_latency_avg_ms, 42.0)
        self.assertEqual(snap.runtime_cost_avg, "0.02")
        blob = str(snap.as_dict()).lower()
        for needle in ("api_key", "sk-", "prompt", "password"):
            self.assertNotIn(needle, blob)

    def test_serialized_stats_no_secrets(self):
        agg = ProviderRuntimeStatsAggregator(RuntimeStatsPolicy(min_samples=1))
        snap = agg.record_success("openai", "premium", latency_ms=10.0)
        blob = str(snap.as_dict()).lower()
        for needle in ("api_key", "authorization", "prompt", "password"):
            self.assertNotIn(needle, blob)


if __name__ == "__main__":
    unittest.main()
