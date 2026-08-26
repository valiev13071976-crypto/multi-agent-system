"""P0.3 budget-aware routing — offline only. No reservation during routing."""

from __future__ import annotations

from decimal import Decimal
import asyncio
import unittest
from unittest.mock import MagicMock

from agents.model_profile import build_model_profile
from agents.model_router import (
    REASON_ALL_AVAILABLE_PROVIDERS,
    REASON_AUTO_BUDGET_MATCH,
    REASON_AUTO_CAPABILITY_MATCH,
    REASON_EXPLICIT_PROVIDER,
    BudgetRoutingDeniedError,
    ModelRouter,
)
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.routing_requirements import CAPABILITY_CODING, TaskRequirements
from agents.core.expert_manager import ExpertManager, FinOpsBudgetDeniedError
from finops.budget_guard import BudgetGuard
from finops.budget_models import BudgetConstraints, BudgetPolicy, SCOPE_GLOBAL
from finops.models import UNKNOWN_COST_ALLOW, UNKNOWN_COST_DENY, BudgetLimits, PriceQuote
from finops.service import FinOpsService


def _profile(
    provider_id: str,
    model: str,
    *,
    coding=True,
    categories="general,technical",
    quality="standard",
    cost="standard",
):
    return build_model_profile(
        provider_id,
        model,
        task_categories_raw=categories,
        coding_raw="true" if coding else "false",
        quality_raw=quality,
        cost_raw=cost,
    )


def _registry(
    *,
    openai_coding=True,
    anthropic_coding=True,
    order=("openai", "anthropic"),
    policy="quality",
    fallback="error",
):
    profiles = {
        "openai": _profile(
            "openai", "premium", coding=openai_coding, quality="premium", cost="premium"
        ),
        "anthropic": _profile(
            "anthropic",
            "cheap",
            coding=anthropic_coding,
            quality="standard",
            cost="cheap",
        ),
    }
    records = {
        pid: ProviderRecord(pid, profiles[pid].model_id, True)
        for pid in ("openai", "anthropic")
    }
    for pid in ("gemini", "grok", "deepseek", "moonshot", "mistral"):
        records[pid] = ProviderRecord(pid, f"{pid}-m", False)
        profiles[pid] = _profile(pid, f"{pid}-m", categories="general")
    return ProviderRegistry(
        records,
        profiles=profiles,
        auto_provider_order=order,
        auto_routing_policy=policy,
        auto_capability_fallback=fallback,
    )


def _finops(prices, *, unknown=UNKNOWN_COST_ALLOW, per_task=None):
    return FinOpsService(
        prices=prices,
        limits=BudgetLimits(per_task, None, None, unknown),
    )


def _guard(finops, *, hard=Decimal("20"), spent=Decimal("0")):
    guard = BudgetGuard(
        finops=finops,
        policies=(
            BudgetPolicy(
                "g",
                SCOPE_GLOBAL,
                hard_limit=hard,
            ),
        ),
        required=True,
    )
    if spent:
        guard.store.add_spent("global:", spent)
    return guard


class BudgetAwareRoutingTests(unittest.TestCase):
    def test_affordable_preferred_remains_selected(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("1"), Decimal("1"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("20"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        decision = ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        self.assertEqual(decision.provider_ids, ("openai",))
        self.assertEqual(decision.reason, REASON_AUTO_CAPABILITY_MATCH)

    def test_over_budget_prefers_cheaper_capable(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        # default estimate tokens 1000+500 → openai ~30, anthropic ~0.0015
        guard = _guard(finops, hard=Decimal("1"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        self.assertIn("openai", constraints.excluded_providers)
        decision = ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))
        self.assertEqual(decision.reason, REASON_AUTO_BUDGET_MATCH)

    def test_cheaper_without_capability_not_selected(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("1"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        req = TaskRequirements(required_capabilities=(CAPABILITY_CODING,))
        with self.assertRaises(BudgetRoutingDeniedError) as ctx:
            ModelRouter(_registry(anthropic_coding=False)).decide(
                "auto",
                "technical",
                category="technical",
                budget_constraints=constraints,
                requirements=req,
            )
        self.assertEqual(ctx.exception.reason, "budget_no_affordable_capable_route")

    def test_all_capable_over_budget_structured_failure(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("10000"), Decimal("10000"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("1"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        with self.assertRaises(BudgetRoutingDeniedError):
            ModelRouter(_registry()).decide(
                "auto",
                "technical",
                category="technical",
                budget_constraints=constraints,
            )

    def test_explicit_over_budget_not_rerouted(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("1"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        with self.assertRaises(BudgetRoutingDeniedError) as ctx:
            ModelRouter(_registry()).decide(
                "openai",
                "technical",
                category="technical",
                budget_constraints=constraints,
            )
        self.assertEqual(ctx.exception.provider, "openai")

    def test_explicit_affordable_works(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("20"))
        constraints = guard.routing_constraints(
            task_id="t", candidates=(("openai", "premium"),)
        )
        decision = ModelRouter(_registry()).decide(
            "openai",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        self.assertEqual(decision.provider_ids, ("openai",))
        self.assertEqual(decision.reason, REASON_EXPLICIT_PROVIDER)

    def test_no_budget_constraint_preserves_p02(self):
        reg = _registry()
        with_none = ModelRouter(reg).decide("auto", "technical", category="technical")
        without = ModelRouter(reg).decide(
            "auto", "technical", category="technical", budget_constraints=None
        )
        self.assertEqual(with_none.provider_ids, without.provider_ids)
        self.assertEqual(with_none.provider_ids, ("openai",))
        self.assertEqual(with_none.reason, REASON_AUTO_CAPABILITY_MATCH)

    def test_unknown_price_not_zero_deny(self):
        finops = _finops(
            {
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            },
            unknown=UNKNOWN_COST_DENY,
        )
        # openai has no quote → unknown
        guard = _guard(finops, hard=Decimal("20"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        self.assertIn("openai", constraints.excluded_providers)
        self.assertIsNone(constraints.candidate_costs.get("openai"))
        decision = ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))

    def test_unknown_price_allow_keeps_candidate(self):
        finops = _finops({}, unknown=UNKNOWN_COST_ALLOW)
        guard = _guard(finops, hard=Decimal("20"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        self.assertNotIn("openai", constraints.excluded_providers)
        decision = ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        self.assertEqual(decision.provider_ids, ("openai",))

    def test_routing_estimation_does_not_reserve(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("20"))
        before = guard.ledger.get_reserved("global")
        constraints = guard.routing_constraints(
            task_id="t", candidates=(("openai", "premium"),)
        )
        ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        after = guard.ledger.get_reserved("global")
        self.assertEqual(before, after)
        self.assertEqual(after, Decimal("0"))
        self.assertTrue(constraints.metadata_safe.get("read_only"))

    def test_execution_budget_guard_still_enforces(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("1"))

        class Agent:
            model = "premium"

            async def run(self, prompt):
                return "ok"

        manager = ExpertManager(openai=Agent(), finops=finops, budget_guard=guard)

        with self.assertRaises(FinOpsBudgetDeniedError):
            asyncio.run(
                manager.run("hi", selected=[("openai", Agent())], task_id="t-exec")
            )

    def test_both_ignores_routing_budget(self):
        constraints = BudgetConstraints(excluded_providers=("openai", "anthropic"))
        decision = ModelRouter(_registry()).decide(
            "both",
            "strategist",
            budget_constraints=constraints,
        )
        self.assertEqual(decision.reason, REASON_ALL_AVAILABLE_PROVIDERS)
        self.assertIn("openai", decision.provider_ids)
        self.assertIn("anthropic", decision.provider_ids)

    def test_router_v2_passes_routing_constraints(self):
        from agents.router_v2 import RouterV2

        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("1"))
        router = RouterV2.__new__(RouterV2)
        router.budget_guard = guard
        router.finops = finops
        router.provider_registry = _registry()
        router.model_router = ModelRouter(router.provider_registry)
        router.last_task_id = "t-v2"
        captured = {}

        def fake_decide(**kwargs):
            captured.update(kwargs)
            return MagicMock(provider_ids=("anthropic",), reason="auto_budget_match")

        router.model_router.decide = fake_decide
        # Exercise the plumbing helper path used in run():
        candidates = tuple(
            (pid, router.provider_registry.model(pid))
            for pid in router.provider_registry.available_provider_ids()
        )
        constraints = router.budget_guard.routing_constraints(
            task_id=router.last_task_id, candidates=candidates
        )
        router.model_router.decide(
            mode="auto",
            role_id="technical",
            category="technical",
            requirements=None,
            budget_constraints=constraints,
        )
        self.assertIsNotNone(captured.get("budget_constraints"))
        self.assertIn("openai", captured["budget_constraints"].excluded_providers)


if __name__ == "__main__":
    unittest.main()
