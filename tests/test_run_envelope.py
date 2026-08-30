"""P1-ENVELOPE — RunEnvelope contract, propagation, and concurrency."""

from __future__ import annotations

import asyncio
import dataclasses
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from agents.core.expert_manager import ExpertManager
from agents.core.pipeline import Pipeline
from agents.provider_result import ProviderResult
from agents.router_v2 import RouterV2
from finops.budget_guard import BudgetGuard
from finops.budget_models import SCOPE_GLOBAL, SCOPE_TENANT, BudgetPolicy
from finops.models import UNKNOWN_COST_ALLOW, BudgetLimits, PriceQuote
from finops.service import FinOpsService
from observability.runtime import build_observability_runtime
from workflow.engine import WorkflowEngine
from workflow.run_envelope import RunEnvelope, RunEnvelopeError


def _sample_envelope(**overrides) -> RunEnvelope:
    base = dict(
        workflow_id="wf-1",
        task_id="task-1",
        tenant_id="tenant-1",
        request_id="req-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        user_id="user-1",
        actor_ref="tenant-1:user-1",
        execution_id="exec-1",
        created_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return RunEnvelope.create(**base)


def _finops(prices):
    return FinOpsService(
        prices=prices,
        limits=BudgetLimits(None, None, None, UNKNOWN_COST_ALLOW),
    )


class _BarrierAgent:
    def __init__(self, provider_id: str, model: str, barrier: asyncio.Barrier, text="ok"):
        self.provider_id = provider_id
        self.model = model
        self.barrier = barrier
        self.text = text

    async def run(self, prompt):
        await self.barrier.wait()
        return ProviderResult(self.text, self.provider_id, self.model, 10, 10, 20)


class RunEnvelopeContractTests(unittest.TestCase):
    def test_1_round_trip_as_dict_from_dict(self):
        original = _sample_envelope(
            deadline_at=datetime(2026, 8, 28, 0, 0, 0, tzinfo=timezone.utc),
            priority="high",
            idempotency_key="idem-1",
            auth_context_version="v1",
            capability_scope_ref="cap-1",
            data_scope_ref="data-1",
        )
        restored = RunEnvelope.from_dict(original.as_dict())
        self.assertEqual(restored.as_dict(), original.as_dict())
        self.assertEqual(restored.execution_id, original.execution_id)
        self.assertEqual(restored.tenant_id, original.tenant_id)
        self.assertEqual(restored.correlation_id, original.correlation_id)
        self.assertEqual(restored.trace_id, original.trace_id)
        self.assertEqual(restored.created_at, original.created_at)
        self.assertEqual(restored.deadline_at, original.deadline_at)

    def test_2_immutable_identity(self):
        envelope = _sample_envelope()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            envelope.tenant_id = "hijacked"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            envelope.execution_id = "other"

    def test_3_tenant_identity_fail_closed(self):
        required = (
            "execution_id",
            "request_id",
            "workflow_id",
            "task_id",
            "tenant_id",
            "correlation_id",
            "trace_id",
        )
        for field in required:
            payload = _sample_envelope().as_dict()
            payload[field] = ""
            with self.assertRaises(RunEnvelopeError) as ctx:
                RunEnvelope.from_dict(payload)
            self.assertEqual(ctx.exception.reason_code, "run_envelope_required_field_missing")
            self.assertEqual(ctx.exception.details.get("field"), field)

        payload = _sample_envelope().as_dict()
        del payload["tenant_id"]
        with self.assertRaises(RunEnvelopeError):
            RunEnvelope.from_dict(payload)

    def test_4_unknown_schema_rejected(self):
        payload = _sample_envelope().as_dict()
        payload["schema_version"] = "999"
        with self.assertRaises(RunEnvelopeError) as ctx:
            RunEnvelope.from_dict(payload)
        self.assertEqual(
            ctx.exception.reason_code, "run_envelope_unknown_schema_version"
        )


class RunEnvelopePropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_5_e2e_same_envelope_no_regeneration(self):
        obs = build_observability_runtime(env={})
        engine = WorkflowEngine(observability=obs)
        seen = {}

        class CM:
            async def prepare(self, prompt, **kwargs):
                return prompt

        class CapturingExpert:
            observability = None

            async def run(self, prompt, **kwargs):
                seen["expert_envelope"] = kwargs.get("envelope")
                seen["expert_task"] = kwargs.get("task_id")
                seen["expert_tenant"] = kwargs.get("tenant_id")
                return {"openai": "ok"}

        class CapturingPipeline(Pipeline):
            def __init__(self):
                # Minimal stub — only execute_experts path used via Router fake.
                self.expert_manager = CapturingExpert()
                self.peer_review = None
                self.fact_validator = None
                self.judge = None
                self.response_formatter = None
                self.supervisor = None
                self.decision_memory = None
                self.structural_validator = None
                self.consistency_validator = None
                self.last_validation = None

            async def execute(self, prompt, selected=None, **kwargs):
                seen["pipeline_envelope"] = kwargs.get("envelope")
                return await self.expert_manager.run(prompt, selected=selected, **kwargs)

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
        router.pipeline = CapturingPipeline()
        router.task_classifier = None
        router.workflow_engine = engine
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
        router.last_run_envelope = None
        router._agents_for_decision = lambda decision: [("openai", object())]

        async def run_router(**kwargs):
            seen["router_kwargs_envelope"] = kwargs.get("envelope")
            with patch("agents.router_v2.get_role_prompt", return_value="role"), patch(
                "agents.router_v2.compose_prompt", return_value="composed"
            ), patch(
                "agents.router_v2.derive_task_requirements",
                return_value=None,
            ), patch(
                "agents.router_v2.routing_category_for_role",
                return_value="technical",
            ):
                return await router.run(
                    kwargs["prompt"],
                    mode=kwargs.get("mode"),
                    role=kwargs.get("role"),
                    task_id=kwargs.get("task_id"),
                    lifecycle=kwargs.get("lifecycle"),
                    request_id=kwargs.get("request_id"),
                    tenant_id=kwargs.get("tenant_id"),
                    user_id=kwargs.get("user_id"),
                    actor_ref=kwargs.get("actor_ref"),
                    envelope=kwargs.get("envelope"),
                )

        await engine.execute(
            "hello",
            "openai",
            "strategist",
            context_manager=CM(),
            run_router=run_router,
            task_id="task-e2e",
            tenant_id="tenant-e2e",
            request_id="req-e2e",
            user_id="user-e2e",
            actor_ref="tenant-e2e:user-e2e",
        )

        engine_env = engine.last_run_envelope
        self.assertIsInstance(engine_env, RunEnvelope)
        self.assertIs(seen["router_kwargs_envelope"], engine_env)
        self.assertIs(seen["pipeline_envelope"], engine_env)
        self.assertIs(seen["expert_envelope"], engine_env)
        self.assertIs(router.last_run_envelope, engine_env)

        self.assertEqual(engine_env.task_id, "task-e2e")
        self.assertEqual(engine_env.tenant_id, "tenant-e2e")
        self.assertEqual(engine_env.request_id, "req-e2e")
        self.assertEqual(engine_env.correlation_id, "req-e2e")
        self.assertEqual(engine_env.user_id, "user-e2e")
        self.assertEqual(engine_env.actor_ref, "tenant-e2e:user-e2e")
        self.assertEqual(engine_env.workflow_id, engine.last_workflow_id)
        self.assertTrue(engine_env.execution_id)
        self.assertTrue(engine_env.trace_id)

        # Same object identity — no regeneration downstream.
        self.assertEqual(seen["expert_task"], engine_env.task_id)
        self.assertEqual(seen["expert_tenant"], engine_env.tenant_id)

        state = engine.state_manager.get(engine.last_workflow_id)
        stored = (state.metadata or {}).get("run_envelope")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["execution_id"], engine_env.execution_id)

    async def test_6_envelope_wins_over_conflicting_kwargs(self):
        envelope = _sample_envelope(
            task_id="env-task",
            workflow_id="env-wf",
            request_id="env-req",
            tenant_id="env-tenant",
            user_id="env-user",
            actor_ref="env-actor",
            correlation_id="env-corr",
            trace_id="env-trace",
            execution_id="env-exec",
        )
        seen = {}

        class CapturePipe:
            async def execute(self, prompt, selected=None, **kwargs):
                seen["pipeline"] = dict(kwargs)
                seen["pipeline_envelope"] = kwargs.get("envelope")
                return {"summary": "ok", "role": "Judge"}

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
        router.pipeline = CapturePipe()
        router.pipeline.expert_manager = type("EM", (), {"observability": None})()
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
        router.last_run_envelope = None
        router._agents_for_decision = lambda decision: [("openai", object())]

        with patch("agents.router_v2.get_role_prompt", return_value="role"), patch(
            "agents.router_v2.compose_prompt", return_value="composed"
        ), patch(
            "agents.router_v2.derive_task_requirements",
            return_value=None,
        ), patch(
            "agents.router_v2.routing_category_for_role",
            return_value="technical",
        ):
            await router.run(
                "prompt",
                mode="openai",
                role="strategist",
                task_id="legacy-task",
                request_id="legacy-req",
                tenant_id="legacy-tenant",
                user_id="legacy-user",
                actor_ref="legacy-actor",
                envelope=envelope,
            )

        pipe = seen["pipeline"]
        self.assertIs(seen["pipeline_envelope"], envelope)
        self.assertEqual(pipe["task_id"], "env-task")
        self.assertEqual(pipe["workflow_id"], "env-wf")
        self.assertEqual(pipe["request_id"], "env-req")
        self.assertEqual(pipe["tenant_id"], "env-tenant")
        self.assertEqual(pipe["user_id"], "env-user")
        self.assertEqual(pipe["actor_ref"], "env-actor")
        self.assertIs(pipe["envelope"], envelope)
        self.assertNotEqual(pipe["tenant_id"], "legacy-tenant")

        # ExpertManager: conflicting kwargs + envelope → envelope wins.
        class InstantAgent:
            async def run(self, prompt):
                return ProviderResult("ok", "openai", "m", 1, 1, 2)

        manager = ExpertManager(openai=InstantAgent())
        await manager.run(
            "p",
            selected=[("openai", InstantAgent())],
            task_id="legacy-task",
            workflow_id="legacy-wf",
            request_id="legacy-req",
            tenant_id="legacy-tenant",
            user_id="legacy-user",
            actor_ref="legacy-actor",
            envelope=envelope,
        )
        self.assertIs(manager.last_run_envelope, envelope)
        self.assertEqual(manager.last_task_id, "env-task")
        self.assertEqual(manager.last_workflow_id, "env-wf")
        self.assertEqual(manager.last_request_id, "env-req")
        self.assertEqual(manager.last_tenant_id, "env-tenant")
        self.assertEqual(manager.last_user_id, "env-user")
        self.assertEqual(manager.last_actor_ref, "env-actor")


class RunEnvelopeConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_7_concurrent_envelopes_no_swap(self):
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
        manager = ExpertManager(
            openai=agent_a,
            finops=finops,
            budget_guard=guard,
        )
        env_a = _sample_envelope(
            execution_id="exec-a",
            task_id="task-a",
            workflow_id="wf-a",
            request_id="req-a",
            tenant_id="tenant-a",
            user_id="user-a",
            actor_ref="tenant-a:user-a",
            correlation_id="corr-a",
            trace_id="trace-a",
        )
        env_b = _sample_envelope(
            execution_id="exec-b",
            task_id="task-b",
            workflow_id="wf-b",
            request_id="req-b",
            tenant_id="tenant-b",
            user_id="user-b",
            actor_ref="tenant-b:user-b",
            correlation_id="corr-b",
            trace_id="trace-b",
        )
        self.assertNotEqual(env_a.execution_id, env_b.execution_id)

        async def run_a():
            return await manager.run(
                "prompt-a",
                selected=[("openai", agent_a)],
                # Conflicting legacy kwargs — envelope must win.
                task_id="wrong-a",
                tenant_id="wrong-tenant",
                envelope=env_a,
            )

        async def run_b():
            return await manager.run(
                "prompt-b",
                selected=[("openai", agent_b)],
                task_id="wrong-b",
                tenant_id="wrong-tenant",
                envelope=env_b,
            )

        results = await asyncio.gather(run_a(), run_b())
        self.assertEqual(results[0]["openai"], "a")
        self.assertEqual(results[1]["openai"], "b")

        usage = list(finops._store.records())
        by_tenant = {r.tenant_id: r for r in usage}
        self.assertIn("tenant-a", by_tenant)
        self.assertIn("tenant-b", by_tenant)
        self.assertEqual(by_tenant["tenant-a"].task_id, "task-a")
        self.assertEqual(by_tenant["tenant-a"].request_id, "req-a")
        self.assertEqual(by_tenant["tenant-a"].workflow_id, "wf-a")
        self.assertEqual(by_tenant["tenant-a"].user_id, "user-a")
        self.assertEqual(by_tenant["tenant-b"].task_id, "task-b")
        self.assertEqual(by_tenant["tenant-b"].request_id, "req-b")
        self.assertEqual(by_tenant["tenant-b"].workflow_id, "wf-b")
        self.assertEqual(by_tenant["tenant-b"].user_id, "user-b")
        self.assertNotIn("wrong-tenant", by_tenant)


if __name__ == "__main__":
    unittest.main()
