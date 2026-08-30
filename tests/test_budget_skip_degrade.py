"""PATCH-MR-04: SKIP_MODEL + soft DEGRADE routing contract."""

from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal

from agents.core.expert_manager import ExpertManager, FinOpsBudgetDeniedError
from agents.model_profile import build_model_profile
from agents.model_router import (
    BudgetRoutingDeniedError,
    ModelRouter,
    ProviderCapabilityMismatchError,
)
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.provider_result import ProviderResult
from agents.routing_audit import REJECT_BUDGET_SKIP
from agents.routing_requirements import CAPABILITY_CODING, TaskRequirements
from finops.budget_guard import BudgetGuard
from finops.budget_models import (
    DECISION_CONTINUE,
    DECISION_DEGRADE,
    DECISION_SKIP_MODEL,
    DECISION_TERMINATE,
    SCOPE_GLOBAL,
    SCOPE_TENANT,
    BudgetPolicy,
    merge_budget_decision,
)
from finops.models import UNKNOWN_COST_ALLOW, BudgetLimits, PriceQuote
from finops.service import FinOpsService
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime


def _finops(prices):
    return FinOpsService(
        prices=prices,
        limits=BudgetLimits(None, None, None, UNKNOWN_COST_ALLOW),
    )


def _profile(provider_id, model, *, coding=True, cost="standard", quality="standard"):
    return build_model_profile(
        provider_id,
        model,
        task_categories_raw="general,technical",
        coding_raw="true" if coding else "false",
        quality_raw=quality,
        cost_raw=cost,
    )


def _registry(*, openai_coding=True, anthropic_coding=True):
    profiles = {
        "openai": _profile("openai", "premium", coding=openai_coding, cost="premium", quality="premium"),
        "anthropic": _profile(
            "anthropic", "cheap", coding=anthropic_coding, cost="cheap", quality="standard"
        ),
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
    def __init__(self, provider_id: str, model: str):
        self.provider_id = provider_id
        self.model = model
        self.calls = 0

    async def run(self, prompt):
        self.calls += 1
        return ProviderResult("ok", self.provider_id, self.model, 10, 10, 20)


class SkipModelDegradeContractTests(unittest.IsolatedAsyncioTestCase):
    def test_case1_decision_precedence(self):
        self.assertEqual(
            merge_budget_decision(DECISION_CONTINUE, DECISION_SKIP_MODEL),
            DECISION_SKIP_MODEL,
        )
        self.assertEqual(
            merge_budget_decision(DECISION_SKIP_MODEL, DECISION_DEGRADE),
            DECISION_DEGRADE,
        )
        self.assertEqual(
            merge_budget_decision(DECISION_DEGRADE, DECISION_TERMINATE),
            DECISION_TERMINATE,
        )
        self.assertEqual(
            merge_budget_decision(DECISION_SKIP_MODEL, DECISION_TERMINATE),
            DECISION_TERMINATE,
        )
        self.assertEqual(
            merge_budget_decision(DECISION_TERMINATE, DECISION_CONTINUE),
            DECISION_TERMINATE,
        )

    def test_case2_skip_model_exact_candidate(self):
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
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("1")),),
            required=True,
        )
        expensive = guard.estimate_request_cost("openai", "premium")
        cheap = guard.estimate_request_cost("anthropic", "cheap")
        d_exp = guard.evaluate(
            task_id="t",
            provider="openai",
            model="premium",
            estimated_cost=expensive,
        )
        d_cheap = guard.evaluate(
            task_id="t",
            provider="anthropic",
            model="cheap",
            estimated_cost=cheap,
        )
        self.assertEqual(d_exp.decision, DECISION_SKIP_MODEL)
        self.assertEqual(d_exp.excluded_providers, ("openai",))
        self.assertEqual(d_cheap.decision, DECISION_CONTINUE)

    async def test_case3_skip_does_not_terminate_fanout(self):
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
            task_id="t-skip-fan",
        )
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 1)
        self.assertIn("anthropic", experts)
        self.assertEqual(manager.last_guard_decision.decision, DECISION_CONTINUE)

    async def test_case4_global_hard_violation_terminates(self):
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
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("100")),),
            required=True,
        )
        guard._hard_violation = True
        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "cheap")
        manager = ExpertManager(
            openai=openai, anthropic=anthropic, finops=finops, budget_guard=guard
        )
        with self.assertRaises(FinOpsBudgetDeniedError):
            await manager.run(
                "hi",
                selected=[("openai", openai), ("anthropic", anthropic)],
                task_id="t-hard",
            )
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 0)

    def test_case5_soft_degrade_routing(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("5"), Decimal("5"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = BudgetGuard(
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
        guard.store.add_spent("global:", Decimal("10"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        self.assertEqual(constraints.decision, DECISION_DEGRADE)
        self.assertTrue(constraints.preferred_cheaper)
        self.assertEqual(constraints.preferred_cheaper[0][0], "anthropic")
        decision = ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        self.assertEqual(decision.provider_ids, ("anthropic",))

    def test_case6_degrade_capability_safety(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("5"), Decimal("5"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = BudgetGuard(
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
        guard.store.add_spent("global:", Decimal("10"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        req = TaskRequirements(required_capabilities=(CAPABILITY_CODING,))
        # Cheap lacks coding; expensive has it — must not pick incapable cheap.
        with self.assertRaises((BudgetRoutingDeniedError, Exception)):
            ModelRouter(_registry(anthropic_coding=False)).decide(
                "auto",
                "technical",
                category="technical",
                budget_constraints=constraints,
                requirements=req,
            )

    def test_case7_degrade_monotonicity_no_reupgrade(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("5"), Decimal("5"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = BudgetGuard(
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
        guard.store.add_spent("global:", Decimal("10"))
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        self.assertEqual(constraints.decision, DECISION_DEGRADE)
        # preferred_cheaper restricts selection; openai excluded from preference set.
        preferred_ids = {p for p, _ in constraints.preferred_cheaper}
        self.assertEqual(preferred_ids, {"anthropic"})
        self.assertIn("openai", constraints.excluded_providers)
        first = ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        second = ModelRouter(_registry()).decide(
            "auto",
            "technical",
            category="technical",
            budget_constraints=constraints,
        )
        self.assertEqual(first.provider_ids, ("anthropic",))
        self.assertEqual(second.provider_ids, ("anthropic",))

    def test_case8_tenant_plus_degrade(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("5"), Decimal("5"), "USD", True
                ),
                ("anthropic", "cheap"): PriceQuote(
                    "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
                ),
            }
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(
                BudgetPolicy(
                    "t",
                    SCOPE_TENANT,
                    hard_limit=Decimal("20"),
                    soft_limit=Decimal("15"),
                    degrade_threshold=Decimal("15"),
                ),
            ),
            required=True,
        )
        guard.store.add_spent("tenant:tenant-a", Decimal("10"))
        ca = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
            tenant_id="tenant-a",
        )
        cb = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
            tenant_id="tenant-b",
        )
        self.assertEqual(ca.decision, DECISION_DEGRADE)
        self.assertEqual(cb.decision, DECISION_CONTINUE)
        self.assertFalse(cb.preferred_cheaper)

    def test_case9_tenant_plus_skip_model(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
            }
        )
        cost = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("t", SCOPE_TENANT, hard_limit=Decimal("1")),),
            required=True,
        ).estimate_request_cost("openai", "premium")
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("t", SCOPE_TENANT, hard_limit=cost),),
            required=True,
        )
        guard.reserve(
            task_id="seed",
            provider="openai",
            model="premium",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )
        da = guard.evaluate(
            task_id="t-a",
            provider="openai",
            model="premium",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )
        db = guard.evaluate(
            task_id="t-b",
            provider="openai",
            model="premium",
            estimated_cost=cost,
            tenant_id="tenant-b",
        )
        self.assertEqual(da.decision, DECISION_SKIP_MODEL)
        self.assertEqual(db.decision, DECISION_CONTINUE)

    def test_case10_explicit_mode_fail_closed(self):
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
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("1")),),
            required=True,
        )
        constraints = guard.routing_constraints(
            task_id="t",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        with self.assertRaises(BudgetRoutingDeniedError):
            ModelRouter(_registry()).decide(
                "openai",
                "technical",
                category="technical",
                budget_constraints=constraints,
            )

    async def test_case11_mode_both_skip_continue(self):
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
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("1")),),
            required=True,
        )
        decision = ModelRouter(_registry()).decide("both", "strategist")
        self.assertIn("openai", decision.provider_ids)
        self.assertIn("anthropic", decision.provider_ids)
        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "cheap")
        manager = ExpertManager(
            openai=openai, anthropic=anthropic, finops=finops, budget_guard=guard
        )
        experts = await manager.run(
            "hi",
            selected=[(p, {"openai": openai, "anthropic": anthropic}[p]) for p in ("openai", "anthropic")],
            task_id="t-both",
        )
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 1)
        self.assertIn("anthropic", experts)

    def test_case12_audit_reconstruction(self):
        obs = ObservabilityRuntime(
            sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
        )
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
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("1")),),
            required=True,
        )
        constraints = guard.routing_constraints(
            task_id="t-audit",
            candidates=(("openai", "premium"), ("anthropic", "cheap")),
        )
        router = ModelRouter(_registry())
        router.bind_routing_audit(
            request_id="req-audit-04",
            task_id="t-audit",
            tenant_id="tenant-audit",
            observability=obs,
        )
        try:
            decision = router.decide(
                "auto",
                "technical",
                category="technical",
                budget_constraints=constraints,
            )
        finally:
            router.clear_routing_audit()
        self.assertEqual(decision.provider_ids, ("anthropic",))
        self.assertEqual(
            decision.factor_snapshot.extra.get("budget_decision"),
            constraints.decision,
        )
        rejected = [c for c in decision.rejected_candidates if c.provider_id == "openai"]
        self.assertTrue(rejected)
        self.assertEqual(rejected[0].rejection_reason, REJECT_BUDGET_SKIP)
        events = [e for e in obs.list_events() if e.event_type == "provider.selected"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].correlation_id, "req-audit-04")
        self.assertEqual(events[0].metadata_safe.get("request_id"), "req-audit-04")


if __name__ == "__main__":
    unittest.main()
