"""P1-OBS Block 1 — BudgetGuard correlation with parent / RunEnvelope."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from agents.core.expert_manager import ExpertManager
from agents.provider_result import ProviderResult
from finops.budget_guard import BudgetGuard
from finops.budget_models import SCOPE_GLOBAL, SCOPE_TENANT, BudgetPolicy
from finops.models import UNKNOWN_COST_ALLOW, BudgetLimits, PriceQuote
from finops.service import FinOpsService
from observability.context import ObservabilityContext
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime
from workflow.run_envelope import RunEnvelope


def _obs():
    return ObservabilityRuntime(
        sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
    )


def _finops():
    return FinOpsService(
        prices={
            ("openai", "m"): PriceQuote(
                "openai", "m", Decimal("1"), Decimal("1"), "USD", True
            )
        },
        limits=BudgetLimits(None, None, None, UNKNOWN_COST_ALLOW),
    )


def _guard(obs=None, *, tenant=False):
    policies = (BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("10")),)
    if tenant:
        policies = (
            BudgetPolicy("t", SCOPE_TENANT, hard_limit=Decimal("10")),
            BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("100")),
        )
    return BudgetGuard(
        finops=_finops(),
        policies=policies,
        required=True,
        observability=obs,
    )


def _envelope(**overrides) -> RunEnvelope:
    base = dict(
        workflow_id="wf-obs-1",
        task_id="task-obs-1",
        tenant_id="tenant-obs-1",
        request_id="req-obs-1",
        correlation_id="corr-obs-1",
        trace_id="trace-obs-1",
        user_id="user-obs-1",
        actor_ref="tenant-obs-1:user-obs-1",
        execution_id="exec-obs-1",
        created_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return RunEnvelope.create(**base)


class BudgetCorrelationBlock1Tests(unittest.TestCase):
    def test_1_parent_correlation_preserved(self):
        obs = _obs()
        parent = ObservabilityContext.root(
            correlation_id="corr-parent",
            workflow_id="wf-p",
            task_id="task-p",
            tenant_id="tenant-p",
            actor_ref="actor-p",
        )
        obs.bind_workflow_context(parent.workflow_id, parent)
        guard = _guard(obs)
        guard.evaluate(
            task_id="task-p",
            provider="openai",
            model="m",
            estimated_cost=Decimal("1"),
            parent_context=parent,
            workflow_id="wf-p",
            tenant_id="tenant-p",
            actor_ref="actor-p",
        )
        budget_events = [
            e for e in obs.list_events() if e.event_type.startswith("budget.")
        ]
        self.assertTrue(budget_events)
        for event in budget_events:
            self.assertEqual(event.correlation_id, "corr-parent")
            self.assertEqual(event.trace_id, parent.trace_id)

    def test_2_workflow_task_tenant_lineage(self):
        obs = _obs()
        parent = ObservabilityContext.root(
            correlation_id="corr-lineage",
            workflow_id="wf-lineage",
            task_id="task-lineage",
            tenant_id="tenant-lineage",
            actor_ref="actor-lineage",
        )
        obs.bind_workflow_context(parent.workflow_id, parent)
        envelope = _envelope(
            workflow_id="wf-lineage",
            task_id="task-lineage",
            tenant_id="tenant-lineage",
            correlation_id="corr-lineage",
            trace_id=parent.trace_id,
            actor_ref="actor-lineage",
            request_id="corr-lineage",
            execution_id="exec-lineage",
        )
        guard = _guard(obs, tenant=True)
        guard.evaluate(
            task_id="ignored-task",
            provider="openai",
            model="m",
            estimated_cost=Decimal("1"),
            tenant_id="ignored-tenant",
            envelope=envelope,
        )
        events = [e for e in obs.list_events() if e.event_type == "budget.evaluated"]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.correlation_id, "corr-lineage")
        self.assertEqual(event.trace_id, parent.trace_id)
        self.assertEqual(event.workflow_id, "wf-lineage")
        self.assertEqual(event.task_id, "task-lineage")
        self.assertEqual(event.metadata_safe.get("tenant_id"), "tenant-lineage")
        self.assertEqual(event.metadata_safe.get("actor_ref"), "actor-lineage")

    def test_3_parent_context_no_independent_root(self):
        obs = _obs()
        parent = ObservabilityContext.root(
            correlation_id="corr-root",
            workflow_id="wf-root",
            task_id="task-root",
        )
        guard = _guard(obs)
        create_calls = []
        original = obs.create_context

        def tracking_create(**kwargs):
            create_calls.append(dict(kwargs))
            return original(**kwargs)

        with patch.object(obs, "create_context", side_effect=tracking_create):
            guard.evaluate(
                task_id="task-root",
                provider="openai",
                model="m",
                estimated_cost=Decimal("1"),
                parent_context=parent,
            )
        self.assertEqual(create_calls, [])
        events = [e for e in obs.list_events() if e.event_type.startswith("budget.")]
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event.correlation_id, parent.correlation_id)
            self.assertEqual(event.trace_id, parent.trace_id)
            self.assertEqual(event.parent_span_id, parent.span_id)

    def test_5_legacy_without_parent_still_works(self):
        obs = _obs()
        guard = _guard(obs)
        decision = guard.evaluate(
            task_id="t-legacy",
            provider="openai",
            model="m",
            estimated_cost=Decimal("1"),
        )
        self.assertEqual(decision.decision, "CONTINUE")
        types = {e.event_type for e in obs.list_events()}
        self.assertIn("budget.evaluated", types)
        events = [e for e in obs.list_events() if e.event_type == "budget.evaluated"]
        self.assertEqual(events[0].task_id, "t-legacy")
        self.assertTrue(events[0].correlation_id)
        self.assertTrue(events[0].trace_id)


class BudgetCorrelationConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_4_concurrent_tenants_no_budget_correlation_swap(self):
        obs = _obs()
        prices = {
            ("openai", "m"): PriceQuote(
                "openai", "m", Decimal("100"), Decimal("100"), "USD", True
            ),
        }
        finops = FinOpsService(
            prices=prices,
            limits=BudgetLimits(None, None, None, UNKNOWN_COST_ALLOW),
        )
        guard = BudgetGuard(
            finops=finops,
            policies=(
                BudgetPolicy("t", SCOPE_TENANT, hard_limit=Decimal("10")),
                BudgetPolicy("g", SCOPE_GLOBAL, hard_limit=Decimal("100")),
            ),
            required=True,
            observability=obs,
        )
        barrier = asyncio.Barrier(2)

        class Agent:
            def __init__(self, text):
                self.model = "m"
                self.text = text

            async def run(self, prompt):
                await barrier.wait()
                return ProviderResult(self.text, "openai", "m", 10, 10, 20)

        manager = ExpertManager(
            openai=Agent("a"),
            finops=finops,
            budget_guard=guard,
        )
        manager.observability = obs

        env_a = _envelope(
            execution_id="exec-a",
            workflow_id="wf-a",
            task_id="task-a",
            tenant_id="tenant-a",
            request_id="req-a",
            correlation_id="corr-a",
            trace_id="trace-a",
            actor_ref="tenant-a:user-a",
            user_id="user-a",
        )
        env_b = _envelope(
            execution_id="exec-b",
            workflow_id="wf-b",
            task_id="task-b",
            tenant_id="tenant-b",
            request_id="req-b",
            correlation_id="corr-b",
            trace_id="trace-b",
            actor_ref="tenant-b:user-b",
            user_id="user-b",
        )
        obs.bind_workflow_context(
            "wf-a",
            ObservabilityContext(
                correlation_id="corr-a",
                trace_id="trace-a",
                span_id="span-a",
                workflow_id="wf-a",
                task_id="task-a",
                tenant_id="tenant-a",
                actor_ref="tenant-a:user-a",
            ),
        )
        obs.bind_workflow_context(
            "wf-b",
            ObservabilityContext(
                correlation_id="corr-b",
                trace_id="trace-b",
                span_id="span-b",
                workflow_id="wf-b",
                task_id="task-b",
                tenant_id="tenant-b",
                actor_ref="tenant-b:user-b",
            ),
        )

        await asyncio.gather(
            manager.run(
                "a",
                selected=[("openai", Agent("a"))],
                envelope=env_a,
            ),
            manager.run(
                "b",
                selected=[("openai", Agent("b"))],
                envelope=env_b,
            ),
        )

        budget_events = [
            e for e in obs.list_events() if e.event_type.startswith("budget.")
        ]
        self.assertTrue(budget_events)
        by_corr = {}
        for event in budget_events:
            by_corr.setdefault(event.correlation_id, []).append(event)
        self.assertIn("corr-a", by_corr)
        self.assertIn("corr-b", by_corr)
        for event in by_corr["corr-a"]:
            self.assertEqual(event.trace_id, "trace-a")
            self.assertEqual(event.workflow_id, "wf-a")
            self.assertEqual(event.task_id, "task-a")
            self.assertEqual(event.metadata_safe.get("tenant_id"), "tenant-a")
        for event in by_corr["corr-b"]:
            self.assertEqual(event.trace_id, "trace-b")
            self.assertEqual(event.workflow_id, "wf-b")
            self.assertEqual(event.task_id, "task-b")
            self.assertEqual(event.metadata_safe.get("tenant_id"), "tenant-b")


if __name__ == "__main__":
    unittest.main()
