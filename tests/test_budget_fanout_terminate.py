"""PATCH-MR-01: multi-provider BudgetGuard TERMINATE must not abort affordable peers."""

from __future__ import annotations

import unittest
from decimal import Decimal

from agents.core.expert_manager import ExpertManager, FinOpsBudgetDeniedError
from agents.provider_result import ProviderResult
from finops.budget_guard import BudgetGuard
from finops.budget_models import BudgetPolicy, SCOPE_GLOBAL
from finops.models import BudgetLimits, PriceQuote, UNKNOWN_COST_ALLOW
from finops.service import FinOpsService


def _finops(prices):
    return FinOpsService(
        prices=prices,
        limits=BudgetLimits(None, None, None, UNKNOWN_COST_ALLOW),
    )


def _guard(finops, *, hard: Decimal) -> BudgetGuard:
    return BudgetGuard(
        finops=finops,
        policies=(BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=hard),),
        required=True,
    )


class _Agent:
    def __init__(self, provider_id: str, model: str, text: str = "ok"):
        self.provider_id = provider_id
        self.model = model
        self.text = text
        self.calls = 0

    async def run(self, prompt):
        self.calls += 1
        return ProviderResult(
            self.text,
            self.provider_id,
            self.model,
            10,
            10,
            20,
        )


class BudgetFanoutTerminateTests(unittest.IsolatedAsyncioTestCase):
    async def test_case1_expensive_terminate_does_not_block_cheap(self):
        """Expensive TERMINATE must not abort affordable cheap peer."""
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
        # Default estimate (1000+500)/1e6 * price → openai ~15, anthropic ~0.0015
        guard = _guard(finops, hard=Decimal("1"))
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
            task_id="t-fanout-1",
        )

        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 1)
        self.assertIn("anthropic", experts)
        self.assertNotIn("openai", experts)
        self.assertIn("anthropic", manager.last_reservations)
        self.assertNotIn("openai", manager.last_reservations)
        reserved = guard.ledger.get_reserved("global")
        # After successful run, reservation is committed/released — reserved may be 0.
        # Ensure we never reserved the expensive provider and spend stayed under hard.
        spent = guard.ledger.get_spent("global")
        self.assertLessEqual(spent + reserved, Decimal("1"))

    async def test_case2_all_unaffordable_terminal(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
                ("anthropic", "also"): PriceQuote(
                    "anthropic", "also", Decimal("10000"), Decimal("10000"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("1"))
        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "also")
        manager = ExpertManager(
            openai=openai,
            anthropic=anthropic,
            finops=finops,
            budget_guard=guard,
        )

        with self.assertRaises(FinOpsBudgetDeniedError):
            await manager.run(
                "hi",
                selected=[("openai", openai), ("anthropic", anthropic)],
                task_id="t-fanout-2",
            )
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 0)
        self.assertEqual(manager.provider_calls, 0)

    async def test_case3_sticky_hard_violation_terminal(self):
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
        guard = _guard(finops, hard=Decimal("100"))
        guard._hard_violation = True
        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "cheap")
        manager = ExpertManager(
            openai=openai,
            anthropic=anthropic,
            finops=finops,
            budget_guard=guard,
        )

        with self.assertRaises(FinOpsBudgetDeniedError) as ctx:
            await manager.run(
                "hi",
                selected=[("openai", openai), ("anthropic", anthropic)],
                task_id="t-fanout-3",
            )
        self.assertEqual(ctx.exception.reason, "budget_hard_limit_exceeded")
        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 0)

    async def test_case4_single_provider_unaffordable_terminal(self):
        finops = _finops(
            {
                ("openai", "premium"): PriceQuote(
                    "openai", "premium", Decimal("10000"), Decimal("10000"), "USD", True
                ),
            }
        )
        guard = _guard(finops, hard=Decimal("1"))
        openai = _Agent("openai", "premium")
        manager = ExpertManager(openai=openai, finops=finops, budget_guard=guard)

        with self.assertRaises(FinOpsBudgetDeniedError) as ctx:
            await manager.run(
                "hi",
                selected=[("openai", openai)],
                task_id="t-fanout-4",
            )
        self.assertEqual(ctx.exception.reason, "budget_hard_limit_exceeded")
        self.assertEqual(openai.calls, 0)

    async def test_case5_reservation_consumes_budget_for_later_peer(self):
        """First affordable reserve can make a later peer unaffordable."""
        finops = _finops(
            {
                ("openai", "a"): PriceQuote(
                    "openai", "a", Decimal("400"), Decimal("400"), "USD", True
                ),
                ("anthropic", "b"): PriceQuote(
                    "anthropic", "b", Decimal("400"), Decimal("400"), "USD", True
                ),
            }
        )
        # Each estimate ≈ 1500/1e6 * 400 = 0.6; hard=1 → first fits, second does not.
        guard = _guard(finops, hard=Decimal("1"))
        openai = _Agent("openai", "a")
        anthropic = _Agent("anthropic", "b")
        manager = ExpertManager(
            openai=openai,
            anthropic=anthropic,
            finops=finops,
            budget_guard=guard,
        )

        experts = await manager.run(
            "hi",
            selected=[("openai", openai), ("anthropic", anthropic)],
            task_id="t-fanout-5",
        )

        self.assertEqual(openai.calls, 1)
        self.assertEqual(anthropic.calls, 0)
        self.assertIn("openai", experts)
        self.assertNotIn("anthropic", experts)
        self.assertIn("openai", manager.last_reservations)
        self.assertNotIn("anthropic", manager.last_reservations)
        reserved = guard.ledger.get_reserved("global")
        spent = guard.ledger.get_spent("global")
        self.assertLessEqual(reserved + spent, Decimal("1"))

    async def test_case6_mode_both_integration_closes_p0(self):
        """mode=both still fans out at route time; execution keeps affordable peer."""
        from agents.model_router import REASON_ALL_AVAILABLE_PROVIDERS, ModelRouter
        from tests.test_budget_aware_routing import _registry

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

        # RouterV2 mode=both: ModelRouter returns all available (no routing budget filter).
        decision = ModelRouter(_registry()).decide("both", "strategist")
        self.assertEqual(decision.reason, REASON_ALL_AVAILABLE_PROVIDERS)
        self.assertIn("openai", decision.provider_ids)
        self.assertIn("anthropic", decision.provider_ids)

        # Same selected fan-out ExpertManager receives after RouterV2._agents_for_decision.
        openai = _Agent("openai", "premium")
        anthropic = _Agent("anthropic", "cheap")
        agents = {"openai": openai, "anthropic": anthropic}
        selected = [
            (pid, agents[pid])
            for pid in decision.provider_ids
            if pid in agents
        ]
        self.assertEqual([p for p, _ in selected], ["openai", "anthropic"])

        manager = ExpertManager(
            openai=openai,
            anthropic=anthropic,
            finops=finops,
            budget_guard=guard,
        )
        experts = await manager.run(
            "hi",
            selected=selected,
            task_id="t-both-6",
            request_id="req-both-6",
            tenant_id="tenant-a",
        )

        self.assertEqual(openai.calls, 0)
        self.assertEqual(anthropic.calls, 1)
        self.assertIn("anthropic", experts)
        self.assertNotIn("openai", experts)


if __name__ == "__main__":
    unittest.main()
