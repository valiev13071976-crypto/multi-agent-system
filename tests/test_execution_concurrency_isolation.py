"""P0 — concurrent ExpertManager / RouterV2 identity isolation."""

from __future__ import annotations

import asyncio
import unittest
from decimal import Decimal
from unittest.mock import patch

from agents.core.expert_manager import ExpertManager
from agents.provider_result import ProviderResult
from agents.router_v2 import RouterV2
from finops.budget_guard import BudgetGuard
from finops.budget_models import SCOPE_GLOBAL, SCOPE_TENANT, BudgetPolicy
from finops.models import UNKNOWN_COST_ALLOW, BudgetLimits, PriceQuote
from finops.service import FinOpsService


def _finops(prices):
    return FinOpsService(
        prices=prices,
        limits=BudgetLimits(None, None, None, UNKNOWN_COST_ALLOW),
    )


class _BarrierAgent:
    """Provider that waits on a barrier so concurrent runs overlap."""

    def __init__(self, provider_id: str, model: str, barrier: asyncio.Barrier, text="ok"):
        self.provider_id = provider_id
        self.model = model
        self.barrier = barrier
        self.text = text
        self.calls = 0
        self.entered = asyncio.Event()

    async def run(self, prompt):
        self.calls += 1
        self.entered.set()
        await self.barrier.wait()
        return ProviderResult(self.text, self.provider_id, self.model, 10, 10, 20)


class ConcurrentExpertManagerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dual_tenant_usage_and_reservations_isolated(self):
        prices = {
            ("openai", "m"): PriceQuote(
                "openai", "m", Decimal("100"), Decimal("100"), "USD", True
            ),
        }
        finops = _finops(prices)
        guard = BudgetGuard(
            finops=finops,
            policies=(
                BudgetPolicy("tenant", SCOPE_TENANT, hard_limit=Decimal("10")),
                BudgetPolicy("global", SCOPE_GLOBAL, hard_limit=Decimal("100")),
            ),
            required=True,
        )
        barrier = asyncio.Barrier(2)
        agent_a = _BarrierAgent("openai", "m", barrier, text="a")
        agent_b = _BarrierAgent("openai", "m", barrier, text="b")
        # Shared manager (process-global style) with two agent slots via selected.
        manager = ExpertManager(
            openai=agent_a,
            finops=finops,
            budget_guard=guard,
        )

        async def run_a():
            return await manager.run(
                "prompt-a",
                selected=[("openai", agent_a)],
                task_id="task-a",
                workflow_id="wf-a",
                request_id="req-a",
                tenant_id="tenant-a",
                user_id="user-a",
                actor_ref="tenant-a:user-a",
            )

        async def run_b():
            # Distinct agent instance so barrier is shared but reservation keys ok.
            manager_b_agent = agent_b
            return await manager.run(
                "prompt-b",
                selected=[("openai", manager_b_agent)],
                task_id="task-b",
                workflow_id="wf-b",
                request_id="req-b",
                tenant_id="tenant-b",
                user_id="user-b",
                actor_ref="tenant-b:user-b",
            )

        results = await asyncio.gather(run_a(), run_b())
        self.assertEqual(results[0]["openai"], "a")
        self.assertEqual(results[1]["openai"], "b")
        self.assertEqual(agent_a.calls, 1)
        self.assertEqual(agent_b.calls, 1)

        usage = list(finops._store.records())
        by_tenant = {r.tenant_id: r for r in usage}
        self.assertIn("tenant-a", by_tenant)
        self.assertIn("tenant-b", by_tenant)
        rec_a = by_tenant["tenant-a"]
        rec_b = by_tenant["tenant-b"]
        self.assertEqual(rec_a.user_id, "user-a")
        self.assertEqual(rec_a.request_id, "req-a")
        self.assertEqual(rec_a.workflow_id, "wf-a")
        self.assertEqual(rec_a.task_id, "task-a")
        self.assertEqual(rec_b.user_id, "user-b")
        self.assertEqual(rec_b.request_id, "req-b")
        self.assertEqual(rec_b.workflow_id, "wf-b")
        self.assertEqual(rec_b.task_id, "task-b")

        # Reservations committed (reconciled) separately under each tenant scope.
        from finops.budget_ledger import scope_ref

        spent_a = guard.store.get_totals(scope_ref(SCOPE_TENANT, "tenant-a"))[2]
        spent_b = guard.store.get_totals(scope_ref(SCOPE_TENANT, "tenant-b"))[2]
        self.assertGreater(spent_a, 0)
        self.assertGreater(spent_b, 0)


class ConcurrentRouterV2PropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_router_pipeline_kwargs_not_swapped(self):
        barrier = asyncio.Barrier(2)
        seen = {}

        class FakePipeline:
            async def execute(self, prompt, selected=None, **kwargs):
                tenant = kwargs.get("tenant_id")
                seen[tenant] = dict(kwargs)
                # Overlap after kwargs captured by execute entry.
                await barrier.wait()
                return {"summary": tenant, "role": "Judge"}

        class FakeRegistry:
            auto_routing_policy = "quality"

            def available_provider_ids(self):
                return ("openai",)

            def model(self, provider_id):
                return "m"

            def status(self):
                return {"openai": True}

        router = RouterV2.__new__(RouterV2)
        router.provider_registry = FakeRegistry()
        router.budget_guard = None
        router.model_router = type(
            "MR",
            (),
            {
                "observability": None,
                "bind_routing_audit": lambda *a, **k: None,
                "clear_routing_audit": lambda *a, **k: None,
                "decide": lambda *a, **k: type(
                    "D",
                    (),
                    {
                        "reason": "auto",
                        "role_id": "strategist",
                        "provider_ids": ("openai",),
                    },
                )(),
            },
        )()
        router.pipeline = FakePipeline()
        router.pipeline.expert_manager = type("EM", (), {"observability": None, "get_provider": lambda self, p: object()})()
        router.task_classifier = None
        router.workflow_engine = type("WE", (), {"observability": None})()
        router.last_classification = None
        router.last_requirements = None
        router.last_route_context = None
        router.last_decision = None
        router.last_task_id = None
        router.last_workflow_id = None
        router.last_request_id = None
        router.last_tenant_id = None
        router.last_user_id = None
        router.last_actor_ref = None

        # Minimal role mapping path: use explicit role (not auto) so classifier unused.
        with patch("agents.router_v2.get_role_prompt", return_value="role"), patch(
            "agents.router_v2.compose_prompt", return_value="composed"
        ), patch(
            "agents.router_v2.derive_task_requirements",
            return_value=None,
        ), patch(
            "agents.router_v2.routing_category_for_role",
            return_value="technical",
        ):
            async def run_a():
                return await router.run(
                    "p",
                    mode="openai",
                    role="strategist",
                    task_id="task-a",
                    request_id="req-a",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    actor_ref="actor-a",
                )

            async def run_b():
                return await router.run(
                    "p",
                    mode="openai",
                    role="strategist",
                    task_id="task-b",
                    request_id="req-b",
                    tenant_id="tenant-b",
                    user_id="user-b",
                    actor_ref="actor-b",
                )

            # Explicit mode uses ProviderNotConfigured if agent None — FakePipeline
            # path uses decide reason auto with agents. Force _agents_for_decision.
            router._agents_for_decision = lambda decision: [("openai", object())]

            await asyncio.gather(run_a(), run_b())

        self.assertEqual(seen["tenant-a"]["tenant_id"], "tenant-a")
        self.assertEqual(seen["tenant-a"]["user_id"], "user-a")
        self.assertEqual(seen["tenant-a"]["request_id"], "req-a")
        self.assertEqual(seen["tenant-a"]["task_id"], "task-a")
        self.assertEqual(seen["tenant-a"]["actor_ref"], "actor-a")

        self.assertEqual(seen["tenant-b"]["tenant_id"], "tenant-b")
        self.assertEqual(seen["tenant-b"]["user_id"], "user-b")
        self.assertEqual(seen["tenant-b"]["request_id"], "req-b")
        self.assertEqual(seen["tenant-b"]["task_id"], "task-b")
        self.assertEqual(seen["tenant-b"]["actor_ref"], "actor-b")


if __name__ == "__main__":
    unittest.main()
