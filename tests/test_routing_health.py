"""P1.1 dynamic provider health gate — offline only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from agents.model_profile import build_model_profile
from agents.model_router import (
    REASON_ALL_AVAILABLE_PROVIDERS,
    REASON_AUTO_CAPABILITY_MATCH,
    REASON_EXPLICIT_PROVIDER,
    ModelRouter,
    NoCapableProviderError,
)
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.routing_audit import REJECT_HEALTH_COOLDOWN
from agents.routing_health import (
    HEALTH_COOLDOWN,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    ProviderHealthTracker,
    RoutingHealthPolicy,
    is_qualifying_provider_failure,
)
from agents.routing_requirements import CAPABILITY_CODING, TaskRequirements
from agents.core.expert_manager import FinOpsBudgetDeniedError


def _profile(provider_id, model, *, coding=True, categories="general,technical", quality="standard"):
    return build_model_profile(
        provider_id,
        model,
        task_categories_raw=categories,
        coding_raw="true" if coding else "false",
        quality_raw=quality,
        cost_raw="standard",
    )


def _registry(*, openai_coding=True, anthropic_coding=True, policy="quality"):
    profiles = {
        "openai": _profile("openai", "premium", coding=openai_coding, quality="premium"),
        "anthropic": _profile(
            "anthropic", "cheap", coding=anthropic_coding, quality="standard"
        ),
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


class _TimeoutExc(TimeoutError):
    pass


class RoutingHealthUnitTests(unittest.TestCase):
    def test_unknown_and_healthy_eligible(self):
        tracker = ProviderHealthTracker(
            RoutingHealthPolicy(failure_threshold=3, cooldown_seconds=60)
        )
        snap = tracker.snapshot("openai", "premium")
        self.assertEqual(snap.state, HEALTH_UNKNOWN)
        self.assertTrue(snap.auto_eligible)

        tracker.record_success("openai", "premium")
        snap = tracker.snapshot("openai", "premium")
        self.assertEqual(snap.state, HEALTH_HEALTHY)
        self.assertTrue(snap.auto_eligible)

    def test_isolated_failure_no_cooldown(self):
        tracker = ProviderHealthTracker(
            RoutingHealthPolicy(failure_threshold=3, cooldown_seconds=60)
        )
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        snap = tracker.snapshot("openai", "premium")
        self.assertEqual(snap.state, HEALTH_DEGRADED)
        self.assertTrue(snap.auto_eligible)
        self.assertNotEqual(snap.state, HEALTH_COOLDOWN)

    def test_repeated_failures_trigger_cooldown(self):
        tracker = ProviderHealthTracker(
            RoutingHealthPolicy(failure_threshold=3, cooldown_seconds=60)
        )
        for _ in range(3):
            tracker.record_failure("openai", "premium", error_class="TimeoutError")
        snap = tracker.snapshot("openai", "premium")
        self.assertEqual(snap.state, HEALTH_COOLDOWN)
        self.assertFalse(snap.auto_eligible)

    def test_cooldown_expires(self):
        tracker = ProviderHealthTracker(
            RoutingHealthPolicy(failure_threshold=2, cooldown_seconds=30)
        )
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tracker.record_failure("openai", "premium", error_class="TimeoutError", now=t0)
        tracker.record_failure(
            "openai",
            "premium",
            error_class="TimeoutError",
            now=t0 + timedelta(seconds=1),
        )
        self.assertEqual(
            tracker.snapshot("openai", "premium", now=t0 + timedelta(seconds=2)).state,
            HEALTH_COOLDOWN,
        )
        after = tracker.snapshot(
            "openai", "premium", now=t0 + timedelta(seconds=40)
        )
        self.assertNotEqual(after.state, HEALTH_COOLDOWN)
        self.assertTrue(after.auto_eligible)

    def test_success_recovers_from_cooldown(self):
        tracker = ProviderHealthTracker(
            RoutingHealthPolicy(failure_threshold=2, cooldown_seconds=600)
        )
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        self.assertEqual(tracker.snapshot("openai", "premium").state, HEALTH_COOLDOWN)
        tracker.record_success("openai", "premium")
        snap = tracker.snapshot("openai", "premium")
        self.assertEqual(snap.state, HEALTH_HEALTHY)
        self.assertTrue(snap.auto_eligible)

    def test_non_provider_errors_ignored(self):
        self.assertFalse(
            is_qualifying_provider_failure(FinOpsBudgetDeniedError("budget"))
        )
        self.assertFalse(
            is_qualifying_provider_failure(NoCapableProviderError("technical"))
        )
        self.assertTrue(is_qualifying_provider_failure(_TimeoutExc()))
        self.assertTrue(is_qualifying_provider_failure(ConnectionError("down")))


class RoutingHealthGateTests(unittest.TestCase):
    def test_healthy_provider_remains_eligible(self):
        tracker = ProviderHealthTracker(RoutingHealthPolicy(failure_threshold=3))
        tracker.record_success("openai", "premium")
        decision = ModelRouter(_registry(), health_tracker=tracker).decide(
            "auto", "technical", category="technical"
        )
        self.assertEqual(decision.provider_ids, ("openai",))

    def test_unknown_health_remains_eligible(self):
        tracker = ProviderHealthTracker(RoutingHealthPolicy(failure_threshold=3))
        decision = ModelRouter(_registry(), health_tracker=tracker).decide(
            "auto", "technical", category="technical"
        )
        self.assertEqual(decision.provider_ids, ("openai",))
        self.assertEqual(decision.reason, REASON_AUTO_CAPABILITY_MATCH)

    def test_cooldown_skipped_selects_next(self):
        tracker = ProviderHealthTracker(RoutingHealthPolicy(failure_threshold=2))
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        decision = ModelRouter(_registry(), health_tracker=tracker).decide(
            "auto", "technical", category="technical"
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))
        rejected = {
            c.provider_id: c.rejection_reason for c in decision.rejected_candidates
        }
        self.assertEqual(rejected.get("openai"), REJECT_HEALTH_COOLDOWN)

    def test_cooldown_unique_capability_no_downgrade(self):
        tracker = ProviderHealthTracker(RoutingHealthPolicy(failure_threshold=2))
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        req = TaskRequirements(required_capabilities=(CAPABILITY_CODING,))
        with self.assertRaises(NoCapableProviderError) as ctx:
            ModelRouter(
                _registry(anthropic_coding=False), health_tracker=tracker
            ).decide(
                "auto",
                "technical",
                category="technical",
                requirements=req,
            )
        self.assertEqual(ctx.exception.reason, "requirements")
        self.assertTrue(
            any(
                r.rejection_reason == REJECT_HEALTH_COOLDOWN
                for r in ctx.exception.candidates_considered
            )
            or any(
                r.rejection_reason == REJECT_HEALTH_COOLDOWN
                for r in ctx.exception.rejected_candidates
            )
            or any(
                r.rejection_reason == "capability_mismatch"
                for r in ctx.exception.rejected_candidates
            )
        )

    def test_explicit_never_silently_replaced(self):
        tracker = ProviderHealthTracker(RoutingHealthPolicy(failure_threshold=2))
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        decision = ModelRouter(_registry(), health_tracker=tracker).decide(
            "openai", "technical", category="technical"
        )
        self.assertEqual(decision.provider_ids, ("openai",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)

    def test_both_ignores_health_gate(self):
        tracker = ProviderHealthTracker(RoutingHealthPolicy(failure_threshold=2))
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        tracker.record_failure("openai", "premium", error_class="TimeoutError")
        decision = ModelRouter(_registry(), health_tracker=tracker).decide(
            "both", "strategist"
        )
        self.assertEqual(decision.reason, REASON_ALL_AVAILABLE_PROVIDERS)
        self.assertIn("openai", decision.provider_ids)
        self.assertIn("anthropic", decision.provider_ids)

    def test_no_health_history_preserves_selection(self):
        reg = _registry()
        without = ModelRouter(reg).decide("auto", "technical", category="technical")
        with_empty = ModelRouter(reg, health_tracker=ProviderHealthTracker()).decide(
            "auto", "technical", category="technical"
        )
        self.assertEqual(without.provider_ids, with_empty.provider_ids)
        self.assertEqual(without.provider_ids, ("openai",))

    def test_factor_snapshot_health_for_selected(self):
        tracker = ProviderHealthTracker(RoutingHealthPolicy(failure_threshold=3))
        tracker.record_success("openai", "premium")
        decision = ModelRouter(_registry(), health_tracker=tracker).decide(
            "auto", "technical", category="technical"
        )
        self.assertEqual(decision.factor_snapshot.health_state, HEALTH_HEALTHY)


if __name__ == "__main__":
    unittest.main()
