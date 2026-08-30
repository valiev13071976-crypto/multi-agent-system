"""PATCH-MR-02: tenant-scoped BudgetGuard isolation and fail-closed missing tenant."""

from __future__ import annotations

import unittest
from decimal import Decimal

from agents.core.expert_manager import ExpertManager, FinOpsBudgetDeniedError
from agents.provider_result import ProviderResult
from finops.budget_guard import REASON_BUDGET_TENANT_REQUIRED, BudgetGuard
from finops.budget_ledger import scope_ref
from finops.budget_models import (
    DECISION_CONTINUE,
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


def _tenant_guard(finops, *, tenant_hard: Decimal, global_hard: Decimal | None = None):
    policies = [
        BudgetPolicy("tenant", SCOPE_TENANT, hard_limit=tenant_hard),
    ]
    if global_hard is not None:
        policies.insert(0, BudgetPolicy("global", SCOPE_GLOBAL, hard_limit=global_hard))
    return BudgetGuard(finops=finops, policies=tuple(policies), required=True)


class _Agent:
    def __init__(self, provider_id: str, model: str, text: str = "ok"):
        self.provider_id = provider_id
        self.model = model
        self.text = text
        self.calls = 0

    async def run(self, prompt):
        self.calls += 1
        return ProviderResult(self.text, self.provider_id, self.model, 10, 10, 20)


class TenantBudgetGuardTests(unittest.IsolatedAsyncioTestCase):
    def _prices_same_model(self):
        return {
            ("openai", "m"): PriceQuote(
                "openai", "m", Decimal("100"), Decimal("100"), "USD", True
            ),
        }

    def _prices_expensive_cheap(self):
        return {
            ("openai", "premium"): PriceQuote(
                "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
            ),
            ("anthropic", "cheap"): PriceQuote(
                "anthropic", "cheap", Decimal("1"), Decimal("1"), "USD", True
            ),
        }

    async def test_case1_tenant_isolation(self):
        finops = _finops(self._prices_same_model())
        # estimate ≈ 1500/1e6 * 100 = 0.15; hard=1 allows several
        guard = _tenant_guard(finops, tenant_hard=Decimal("1"))
        cost = guard.estimate_request_cost("openai", "m")
        self.assertIsNotNone(cost)

        r_a = guard.reserve(
            task_id="t-a",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )
        remaining_b = guard.ledger.get_remaining(
            hard_limit=Decimal("1"), scope=SCOPE_TENANT, key="tenant-b"
        )
        remaining_a = guard.ledger.get_remaining(
            hard_limit=Decimal("1"), scope=SCOPE_TENANT, key="tenant-a"
        )
        self.assertEqual(remaining_b, Decimal("1"))
        self.assertEqual(remaining_a, Decimal("1") - cost)
        self.assertIn(scope_ref(SCOPE_TENANT, "tenant-a"), r_a.scope_refs)
        self.assertNotIn(scope_ref(SCOPE_TENANT, "tenant-b"), r_a.scope_refs)

    async def test_case2_same_tenant_accumulation(self):
        finops = _finops(self._prices_same_model())
        guard = _tenant_guard(finops, tenant_hard=Decimal("1"))
        cost = guard.estimate_request_cost("openai", "m")
        guard.reserve(
            task_id="t1",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )
        before = guard.ledger.get_remaining(
            hard_limit=Decimal("1"), scope=SCOPE_TENANT, key="tenant-a"
        )
        guard.reserve(
            task_id="t2",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )
        after = guard.ledger.get_remaining(
            hard_limit=Decimal("1"), scope=SCOPE_TENANT, key="tenant-a"
        )
        self.assertEqual(after, before - cost)

    async def test_case3_tenant_a_exhausted_b_available(self):
        finops = _finops(self._prices_same_model())
        # hard just above one estimate so second A fails, B still ok
        cost = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("t", SCOPE_TENANT, hard_limit=Decimal("1")),),
            required=True,
        ).estimate_request_cost("openai", "m")
        guard = _tenant_guard(finops, tenant_hard=cost)
        guard.reserve(
            task_id="t-a1",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )
        denied = guard.evaluate(
            task_id="t-a2",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )
        allowed = guard.evaluate(
            task_id="t-b1",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-b",
        )
        self.assertEqual(denied.decision, DECISION_SKIP_MODEL)
        self.assertEqual(allowed.decision, DECISION_CONTINUE)

        agent = _Agent("openai", "m")
        manager = ExpertManager(openai=agent, finops=finops, budget_guard=guard)
        with self.assertRaises(FinOpsBudgetDeniedError):
            await manager.run(
                "hi",
                selected=[("openai", agent)],
                task_id="t-a-exec",
                tenant_id="tenant-a",
            )
        self.assertEqual(agent.calls, 0)

        agent_b = _Agent("openai", "m")
        manager_b = ExpertManager(openai=agent_b, finops=finops, budget_guard=guard)
        experts = await manager_b.run(
            "hi",
            selected=[("openai", agent_b)],
            task_id="t-b-exec",
            tenant_id="tenant-b",
        )
        self.assertEqual(agent_b.calls, 1)
        self.assertIn("openai", experts)

    async def test_case4_missing_tenant_with_tenant_policy_fail_closed(self):
        finops = _finops(self._prices_same_model())
        guard = _tenant_guard(finops, tenant_hard=Decimal("10"))
        d = guard.evaluate(
            task_id="t",
            provider="openai",
            model="m",
            estimated_cost=Decimal("0.1"),
            tenant_id=None,
        )
        self.assertEqual(d.decision, DECISION_TERMINATE)
        self.assertEqual(d.reason_code, REASON_BUDGET_TENANT_REQUIRED)

        agent = _Agent("openai", "m")
        manager = ExpertManager(openai=agent, finops=finops, budget_guard=guard)
        with self.assertRaises(FinOpsBudgetDeniedError) as ctx:
            await manager.run(
                "hi",
                selected=[("openai", agent)],
                task_id="t-miss",
                tenant_id=None,
            )
        self.assertEqual(ctx.exception.reason, REASON_BUDGET_TENANT_REQUIRED)
        self.assertEqual(agent.calls, 0)
        self.assertEqual(guard.ledger.get_reserved(SCOPE_TENANT, ""), Decimal("0"))
        self.assertEqual(guard.ledger.get_reserved(SCOPE_GLOBAL), Decimal("0"))

    async def test_case5_missing_tenant_without_tenant_policy(self):
        finops = _finops(self._prices_same_model())
        guard = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("10")),),
            required=True,
        )
        agent = _Agent("openai", "m")
        manager = ExpertManager(openai=agent, finops=finops, budget_guard=guard)
        experts = await manager.run(
            "hi",
            selected=[("openai", agent)],
            task_id="t-global",
            tenant_id=None,
        )
        self.assertEqual(agent.calls, 1)
        self.assertIn("openai", experts)

    async def test_case6_routing_execution_consistency(self):
        finops = _finops(self._prices_same_model())
        cost = BudgetGuard(
            finops=finops,
            policies=(BudgetPolicy("t", SCOPE_TENANT, hard_limit=Decimal("1")),),
            required=True,
        ).estimate_request_cost("openai", "m")
        # hard == one estimate → after Tenant A reserves once, A is exhausted.
        guard = _tenant_guard(finops, tenant_hard=cost)
        guard.reserve(
            task_id="seed2",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )

        constraints_a = guard.routing_constraints(
            task_id="route-a",
            candidates=(("openai", "m"),),
            tenant_id="tenant-a",
        )
        constraints_b = guard.routing_constraints(
            task_id="route-b",
            candidates=(("openai", "m"),),
            tenant_id="tenant-b",
        )
        self.assertIn("openai", constraints_a.excluded_providers)
        self.assertNotIn("openai", constraints_b.excluded_providers)
        self.assertEqual(constraints_a.metadata_safe.get("tenant_id"), "tenant-a")
        self.assertEqual(constraints_b.metadata_safe.get("tenant_id"), "tenant-b")

        eval_a = guard.evaluate(
            task_id="eval-a",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )
        eval_b = guard.evaluate(
            task_id="eval-b",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-b",
        )
        self.assertEqual(eval_a.decision, DECISION_SKIP_MODEL)
        self.assertEqual(eval_b.decision, DECISION_CONTINUE)

        from finops.budget_guard import BudgetGuardError

        with self.assertRaises(BudgetGuardError):
            guard.reserve(
                task_id="res-a",
                provider="openai",
                model="m",
                estimated_cost=cost,
                tenant_id="tenant-a",
            )
        r_b = guard.reserve(
            task_id="res-b",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-b",
        )
        self.assertEqual(r_b.metadata_safe.get("tenant_id"), "tenant-b")

    async def test_case7_multi_provider_plus_tenant(self):
        finops = _finops(self._prices_expensive_cheap())
        guard = _tenant_guard(finops, tenant_hard=Decimal("1"))
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
            task_id="t-fanout-tenant",
            tenant_id="tenant-a",
        )
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 1)
        self.assertIn("anthropic", experts)
        self.assertEqual(
            guard.ledger.get_spent(SCOPE_TENANT, "tenant-b")
            + guard.ledger.get_reserved(SCOPE_TENANT, "tenant-b"),
            Decimal("0"),
        )
        used_a = guard.ledger.get_spent(SCOPE_TENANT, "tenant-a") + guard.ledger.get_reserved(
            SCOPE_TENANT, "tenant-a"
        )
        self.assertGreater(used_a, Decimal("0"))
        self.assertLessEqual(used_a, Decimal("1"))

    async def test_case8_reservation_lifecycle_attribution(self):
        finops = _finops(self._prices_same_model())
        guard = _tenant_guard(finops, tenant_hard=Decimal("10"))
        cost = guard.estimate_request_cost("openai", "m")
        reservation = guard.reserve(
            task_id="t-life",
            provider="openai",
            model="m",
            estimated_cost=cost,
            tenant_id="tenant-a",
        )
        self.assertEqual(reservation.metadata_safe.get("tenant_id"), "tenant-a")
        self.assertIn(scope_ref(SCOPE_TENANT, "tenant-a"), reservation.scope_refs)

        before_spent = guard.ledger.get_spent(SCOPE_TENANT, "tenant-a")
        reconciled = guard.reconcile(
            reservation.reservation_id,
            actual_cost=cost,
            usage_record_key="usage-1",
        )
        after_spent = guard.ledger.get_spent(SCOPE_TENANT, "tenant-a")
        self.assertEqual(reconciled.metadata_safe.get("tenant_id"), "tenant-a")
        self.assertEqual(after_spent, before_spent + cost)
        self.assertEqual(
            guard.ledger.get_spent(SCOPE_TENANT, "tenant-b"),
            Decimal("0"),
        )

        agent = _Agent("openai", "m")
        manager = ExpertManager(openai=agent, finops=finops, budget_guard=guard)
        await manager.run(
            "hi",
            selected=[("openai", agent)],
            task_id="t-usage",
            tenant_id="tenant-a",
            request_id="req-1",
        )
        self.assertTrue(manager.last_usage)
        self.assertEqual(manager.last_usage[-1].tenant_id, "tenant-a")


if __name__ == "__main__":
    unittest.main()
