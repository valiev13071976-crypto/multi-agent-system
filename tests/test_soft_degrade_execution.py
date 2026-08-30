"""P0 — soft DEGRADE execution composition (ExpertManager capable_candidates)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from agents.core.expert_manager import ExpertManager, FinOpsBudgetDeniedError
from agents.model_profile import build_model_profile
from agents.model_router import ModelRouter
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.provider_result import ProviderResult
from finops.budget_guard import REASON_BUDGET_TENANT_REQUIRED, BudgetGuard
from finops.budget_models import (
    DECISION_CONTINUE,
    DECISION_DEGRADE,
    DECISION_SKIP_MODEL,
    DECISION_TERMINATE,
    SCOPE_GLOBAL,
    SCOPE_TENANT,
    BudgetPolicy,
)
from finops.models import UNKNOWN_COST_ALLOW, BudgetLimits, PriceQuote
from finops.service import FinOpsService


def _finops(prices):
    return FinOpsService(
        prices=prices,
        limits=BudgetLimits(None, None, None, UNKNOWN_COST_ALLOW),
    )


def _expensive_cheap_prices():
    return {
        ("openai", "premium"): PriceQuote(
            "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
        ),
        ("anthropic", "cheap"): PriceQuote(
            "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
        ),
    }


def _soft_hard_guard(finops) -> BudgetGuard:
    """remaining <= soft threshold with hard cap → soft DEGRADE path."""

    return BudgetGuard(
        finops=finops,
        policies=(
            BudgetPolicy(
                "g",
                SCOPE_GLOBAL,
                hard_limit=Decimal("20"),
                soft_limit=Decimal("15"),
                degrade_threshold=Decimal("15"),
            ),
        ),
        required=True,
    )


def _profile(provider_id, model, *, cost="standard", quality="standard"):
    return build_model_profile(
        provider_id,
        model,
        task_categories_raw="general,technical",
        coding_raw="true",
        quality_raw=quality,
        cost_raw=cost,
    )


def _registry():
    profiles = {
        "openai": _profile("openai", "premium", cost="premium", quality="premium"),
        "anthropic": _profile("anthropic", "cheap", cost="cheap", quality="standard"),
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


class _Agent:
    def __init__(self, provider_id: str, model: str, text: str = "ok"):
        self.provider_id = provider_id
        self.model = model
        self.text = text
        self.calls = 0

    async def run(self, prompt):
        self.calls += 1
        return ProviderResult(self.text, self.provider_id, self.model, 10, 10, 20)


class SoftDegradeExecutionCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_soft_degrade_route_then_execute_allows_cheap(self):
        """auto: routing prefers cheap → ExpertManager reserves/runs that provider."""

        finops = _finops(_expensive_cheap_prices())
        guard = _soft_hard_guard(finops)
        # spent=10 → remaining=10 <= soft 15 → DEGRADE
        guard.store.add_spent("global:", Decimal("10"))

        constraints = guard.routing_constraints(
            task_id="t-auto-soft",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        self.assertEqual(constraints.decision, DECISION_DEGRADE)
        self.assertEqual(constraints.preferred_cheaper[0][0], "anthropic")

        decision = ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))

        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "cheap")
        manager = ExpertManager(
            openai=openai,
            anthropic=anthropic,
            finops=finops,
            budget_guard=guard,
        )
        selected = [("anthropic", anthropic)]
        experts = await manager.run(
            "hi",
            selected=selected,
            task_id="t-auto-soft",
        )
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 1)
        self.assertIn("anthropic", experts)
        self.assertIn("anthropic", manager.last_reservations)
        self.assertEqual(manager.last_guard_decision.decision, DECISION_DEGRADE)
        self.assertEqual(manager.last_guard_decision.recommended_provider, "anthropic")

    async def test_both_soft_pressure_affordable_peer_runs(self):
        """mode=both: soft pressure must not false-global-TERMINATE affordable peer."""

        finops = _finops(_expensive_cheap_prices())
        guard = _soft_hard_guard(finops)
        guard.store.add_spent("global:", Decimal("10"))

        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "cheap")
        manager = ExpertManager(
            openai=openai,
            anthropic=anthropic,
            finops=finops,
            budget_guard=guard,
        )
        experts = await manager.run(
            "hi",
            selected=[("openai", openai), ("anthropic", anthropic)],
            task_id="t-both-soft",
        )
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 1)
        self.assertIn("anthropic", experts)
        # Must not collapse to false no-route TERMINATE for the whole set.
        self.assertNotEqual(
            manager.last_guard_decision.reason_code,
            "budget_no_affordable_capable_route",
        )
        self.assertEqual(manager.last_guard_decision.decision, DECISION_DEGRADE)
        self.assertEqual(manager.last_guard_decision.recommended_provider, "anthropic")

    async def test_hard_skip_model_fanout_unchanged(self):
        finops = _finops(_expensive_cheap_prices())
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("1")),),
            required=True,
        )
        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "cheap")
        manager = ExpertManager(
            openai=openai, anthropic=anthropic, finops=finops, budget_guard=guard
        )
        experts = await manager.run(
            "hi",
            selected=[("openai", openai), ("anthropic", anthropic)],
            task_id="t-hard-skip",
        )
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 1)
        self.assertIn("anthropic", experts)
        self.assertEqual(manager.last_guard_decision.decision, DECISION_CONTINUE)

    async def test_sticky_global_terminate_unchanged(self):
        finops = _finops(_expensive_cheap_prices())
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("0.0001")),),
            required=True,
        )
        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "cheap")
        manager = ExpertManager(
            openai=openai, anthropic=anthropic, finops=finops, budget_guard=guard
        )
        # Force sticky hard violation path used by ExpertManager.
        guard._hard_violation = True
        with self.assertRaises(FinOpsBudgetDeniedError) as ctx:
            await manager.run(
                "hi",
                selected=[("openai", openai), ("anthropic", anthropic)],
                task_id="t-sticky",
            )
        self.assertEqual(str(ctx.exception), "budget_hard_limit_exceeded")
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 0)

    async def test_tenant_fail_closed_unchanged(self):
        finops = _finops(_expensive_cheap_prices())
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("t", SCOPE_TENANT, hard_limit=Decimal("10")),),
            required=True,
        )
        openai = _Agent("openai", "premium")
        manager = ExpertManager(
            openai=openai, finops=finops, budget_guard=guard
        )
        with self.assertRaises(FinOpsBudgetDeniedError) as ctx:
            await manager.run(
                "hi",
                selected=[("openai", openai)],
                task_id="t-tenant",
                tenant_id=None,
            )
        self.assertEqual(str(ctx.exception), REASON_BUDGET_TENANT_REQUIRED)
        self.assertEqual(openai.calls, 0)

    async def test_evaluate_and_reserve_share_same_candidate_set(self):
        finops = _finops(_expensive_cheap_prices())
        guard = _soft_hard_guard(finops)
        guard.store.add_spent("global:", Decimal("10"))

        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "cheap")
        manager = ExpertManager(
            openai=openai,
            anthropic=anthropic,
            finops=finops,
            budget_guard=guard,
        )

        seen_eval = []
        seen_reserve = []
        real_evaluate = guard.evaluate
        real_reserve = guard.reserve

        def wrapped_evaluate(*args, **kwargs):
            seen_eval.append(kwargs.get("capable_candidates"))
            return real_evaluate(*args, **kwargs)

        def wrapped_reserve(*args, **kwargs):
            seen_reserve.append(kwargs.get("capable_candidates"))
            return real_reserve(*args, **kwargs)

        with patch.object(guard, "evaluate", side_effect=wrapped_evaluate):
            with patch.object(guard, "reserve", side_effect=wrapped_reserve):
                await manager.run(
                    "hi",
                    selected=[("openai", openai), ("anthropic", anthropic)],
                    task_id="t-same-set",
                )

        self.assertTrue(seen_eval)
        self.assertTrue(seen_reserve)
        expected = (("openai", "premium"), ("anthropic", "cheap"))
        for cand in seen_eval:
            self.assertEqual(cand, expected)
        for cand in seen_reserve:
            self.assertEqual(cand, expected)
        # Outer ExpertManager evaluate calls + reserve's internal evaluates all see set.
        self.assertGreaterEqual(len(seen_eval), 2)
        self.assertEqual(anthropic.calls, 1)


if __name__ == "__main__":
    unittest.main()
