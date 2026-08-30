"""P1-USAGE Block 1 — UsageRecord actor/execution ownership from RunEnvelope."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from agents.core.expert_manager import ExpertManager
from agents.provider_result import ProviderResult
from finops.models import UNKNOWN_COST_ALLOW, BudgetLimits, PriceQuote, UsageRecord
from finops.service import FinOpsService
from finops.storage import InMemoryUsageStore
from workflow.run_envelope import RunEnvelope


def _finops(store=None):
    return FinOpsService(
        prices={
            ("openai", "m"): PriceQuote(
                "openai", "m", Decimal("1"), Decimal("1"), "USD", True
            )
        },
        limits=BudgetLimits(None, None, None, UNKNOWN_COST_ALLOW),
        store=store,
    )


def _envelope(**overrides) -> RunEnvelope:
    base = dict(
        workflow_id="wf-usage-1",
        task_id="task-usage-1",
        tenant_id="tenant-usage-1",
        request_id="req-usage-1",
        correlation_id="corr-usage-1",
        trace_id="trace-usage-1",
        user_id="user-usage-1",
        actor_ref="tenant-usage-1:user-usage-1",
        execution_id="exec-usage-1",
        created_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return RunEnvelope.create(**base)


class _Agent:
    def __init__(self, text="ok"):
        self.model = "m"
        self.text = text

    async def run(self, prompt):
        return ProviderResult(self.text, "openai", "m", 10, 5, 15)


class _BarrierAgent:
    def __init__(self, text, barrier):
        self.model = "m"
        self.text = text
        self.barrier = barrier

    async def run(self, prompt):
        await self.barrier.wait()
        return ProviderResult(self.text, "openai", "m", 10, 5, 15)


class UsageOwnershipBlock1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_1_actor_ownership_from_envelope(self):
        finops = _finops()
        manager = ExpertManager(openai=_Agent(), finops=finops)
        envelope = _envelope(actor_ref="tenant-a:user-a")
        await manager.run(
            "p",
            selected=[("openai", _Agent())],
            # Conflicting legacy kwargs — envelope wins for ownership fields.
            actor_ref="legacy-actor",
            envelope=envelope,
        )
        record = finops._store.records()[0]
        self.assertEqual(record.actor_ref, envelope.actor_ref)
        self.assertEqual(record.actor_ref, "tenant-a:user-a")
        self.assertNotEqual(record.actor_ref, "legacy-actor")

    async def test_2_execution_ownership_from_envelope(self):
        finops = _finops()
        manager = ExpertManager(openai=_Agent(), finops=finops)
        envelope = _envelope(execution_id="exec-owned-42")
        await manager.run(
            "p",
            selected=[("openai", _Agent())],
            envelope=envelope,
        )
        record = finops._store.records()[0]
        self.assertEqual(record.execution_id, envelope.execution_id)
        self.assertEqual(record.execution_id, "exec-owned-42")

    async def test_3_complete_lineage_from_one_execution(self):
        finops = _finops()
        manager = ExpertManager(openai=_Agent(), finops=finops)
        envelope = _envelope(
            workflow_id="wf-line",
            task_id="task-line",
            tenant_id="tenant-line",
            user_id="user-line",
            request_id="req-line",
            actor_ref="tenant-line:user-line",
            execution_id="exec-line",
            correlation_id="corr-line",
            trace_id="trace-line",
        )
        await manager.run(
            "p",
            selected=[("openai", _Agent())],
            task_id="wrong-task",
            tenant_id="wrong-tenant",
            envelope=envelope,
        )
        record = finops._store.records()[0]
        self.assertEqual(record.tenant_id, "tenant-line")
        self.assertEqual(record.user_id, "user-line")
        self.assertEqual(record.actor_ref, "tenant-line:user-line")
        self.assertEqual(record.request_id, "req-line")
        self.assertEqual(record.workflow_id, "wf-line")
        self.assertEqual(record.task_id, "task-line")
        self.assertEqual(record.execution_id, "exec-line")

    async def test_5_legacy_without_envelope_empty_ownership(self):
        finops = _finops()
        manager = ExpertManager(openai=_Agent(), finops=finops)
        await manager.run(
            "p",
            selected=[("openai", _Agent())],
            task_id="task-legacy",
            workflow_id="wf-legacy",
            request_id="req-legacy",
            tenant_id="tenant-legacy",
            user_id="user-legacy",
            actor_ref="tenant-legacy:user-legacy",
        )
        record = finops._store.records()[0]
        self.assertEqual(record.task_id, "task-legacy")
        self.assertEqual(record.tenant_id, "tenant-legacy")
        self.assertEqual(record.user_id, "user-legacy")
        self.assertEqual(record.request_id, "req-legacy")
        self.assertEqual(record.workflow_id, "wf-legacy")
        # Additive ownership fields stay empty without envelope.
        self.assertEqual(record.actor_ref, "")
        self.assertEqual(record.execution_id, "")

    def test_6_persistence_round_trip_additive_fields(self):
        store = InMemoryUsageStore()
        record = UsageRecord(
            task_id="t1",
            provider_id="openai",
            model_id="m",
            input_tokens=1,
            output_tokens=2,
            total_tokens=3,
            estimated_cost=Decimal("0.01"),
            currency="USD",
            timestamp=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
            workflow_id="wf-1",
            tenant_id="tenant-1",
            user_id="user-1",
            request_id="req-1",
            actor_ref="tenant-1:user-1",
            execution_id="exec-1",
        )
        store.add(record)
        loaded = store.records()[0]
        self.assertEqual(loaded.actor_ref, "tenant-1:user-1")
        self.assertEqual(loaded.execution_id, "exec-1")
        self.assertEqual(loaded.tenant_id, "tenant-1")
        self.assertEqual(loaded.workflow_id, "wf-1")
        self.assertEqual(loaded.request_id, "req-1")
        # FinOpsService path also preserves identity.
        finops = _finops(store=InMemoryUsageStore())
        finops.record_usage(record)
        again = finops._store.records()[0]
        self.assertEqual(again.actor_ref, record.actor_ref)
        self.assertEqual(again.execution_id, record.execution_id)


class UsageOwnershipConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_4_concurrent_ownership_no_swap(self):
        store = InMemoryUsageStore()
        finops = _finops(store=store)
        barrier = asyncio.Barrier(2)
        manager = ExpertManager(openai=_Agent(), finops=finops)
        env_a = _envelope(
            execution_id="exec-a",
            workflow_id="wf-a",
            task_id="task-a",
            tenant_id="tenant-a",
            user_id="user-a",
            request_id="req-a",
            actor_ref="tenant-a:user-a",
            correlation_id="corr-a",
            trace_id="trace-a",
        )
        env_b = _envelope(
            execution_id="exec-b",
            workflow_id="wf-b",
            task_id="task-b",
            tenant_id="tenant-b",
            user_id="user-b",
            request_id="req-b",
            actor_ref="tenant-b:user-b",
            correlation_id="corr-b",
            trace_id="trace-b",
        )

        await asyncio.gather(
            manager.run(
                "a",
                selected=[("openai", _BarrierAgent("a", barrier))],
                envelope=env_a,
            ),
            manager.run(
                "b",
                selected=[("openai", _BarrierAgent("b", barrier))],
                envelope=env_b,
            ),
        )

        records = list(store.records())
        self.assertEqual(len(records), 2)
        by_exec = {r.execution_id: r for r in records}
        self.assertIn("exec-a", by_exec)
        self.assertIn("exec-b", by_exec)
        self.assertEqual(by_exec["exec-a"].actor_ref, "tenant-a:user-a")
        self.assertEqual(by_exec["exec-a"].tenant_id, "tenant-a")
        self.assertEqual(by_exec["exec-a"].workflow_id, "wf-a")
        self.assertEqual(by_exec["exec-a"].task_id, "task-a")
        self.assertEqual(by_exec["exec-a"].request_id, "req-a")
        self.assertEqual(by_exec["exec-a"].user_id, "user-a")
        self.assertEqual(by_exec["exec-b"].actor_ref, "tenant-b:user-b")
        self.assertEqual(by_exec["exec-b"].tenant_id, "tenant-b")
        self.assertEqual(by_exec["exec-b"].workflow_id, "wf-b")
        self.assertEqual(by_exec["exec-b"].task_id, "task-b")
        self.assertEqual(by_exec["exec-b"].request_id, "req-b")
        self.assertEqual(by_exec["exec-b"].user_id, "user-b")


if __name__ == "__main__":
    unittest.main()
