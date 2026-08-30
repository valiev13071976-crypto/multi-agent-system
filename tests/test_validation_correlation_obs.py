"""P1-OBS Block 2 — FactValidator validation.completed correlation."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from agents.core.pipeline import Pipeline
from agents.fact_validator import FactValidator
from observability.context import ObservabilityContext
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime
from workflow.run_envelope import RunEnvelope


def _obs():
    return ObservabilityRuntime(
        sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
    )


def _envelope(**overrides) -> RunEnvelope:
    base = dict(
        workflow_id="wf-val-1",
        task_id="task-val-1",
        tenant_id="tenant-val-1",
        request_id="req-val-1",
        correlation_id="corr-val-1",
        trace_id="trace-val-1",
        user_id="user-val-1",
        actor_ref="tenant-val-1:user-val-1",
        execution_id="exec-val-1",
        created_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return RunEnvelope.create(**base)


class _NoopPeer:
    async def review(self, experts, errors=None):
        return {"ok": True}


class _NoopJudge:
    async def run(self, **kwargs):
        return {"summary": "ok", "role": "Judge"}


class _NoopFormatter:
    async def format(self, decision):
        return decision


class _NoopMemory:
    async def save(self, prompt, answer):
        return None


class _Structural:
    def validate_experts(self, experts):
        return {"status": "pass"}


class _Consistency:
    def validate(self, experts):
        return {"status": "unknown"}


class ValidationCorrelationBlock2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_1_validation_correlation_matches_parent(self):
        obs = _obs()
        parent = ObservabilityContext.root(
            correlation_id="corr-parent",
            workflow_id="wf-p",
            task_id="task-p",
            tenant_id="tenant-p",
            actor_ref="actor-p",
        )
        obs.bind_workflow_context(parent.workflow_id, parent)
        validator = FactValidator(observability=obs)
        await validator.validate(
            {"openai": "short"},
            category="strategy",
            parent_context=parent,
            workflow_id="wf-p",
            task_id="task-p",
            tenant_id="tenant-p",
            actor_ref="actor-p",
        )
        events = [
            e for e in obs.list_events() if e.event_type == "validation.completed"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].correlation_id, "corr-parent")
        self.assertEqual(events[0].trace_id, parent.trace_id)

    async def test_2_lineage_fields(self):
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
        validator = FactValidator(observability=obs)
        await validator.validate(
            {"openai": "short"},
            category="strategy",
            envelope=envelope,
        )
        events = [
            e for e in obs.list_events() if e.event_type == "validation.completed"
        ]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.correlation_id, "corr-lineage")
        self.assertEqual(event.trace_id, parent.trace_id)
        self.assertEqual(event.workflow_id, "wf-lineage")
        self.assertEqual(event.task_id, "task-lineage")
        self.assertEqual(event.metadata_safe.get("tenant_id"), "tenant-lineage")
        self.assertEqual(event.metadata_safe.get("actor_ref"), "actor-lineage")
        self.assertEqual(event.metadata_safe.get("validator_type"), "fact")
        blob = str(event.metadata_safe)
        self.assertNotIn("prompt", blob.lower())
        self.assertNotIn("secret", blob.lower())

    async def test_3_no_independent_root_with_parent(self):
        obs = _obs()
        parent = ObservabilityContext.root(
            correlation_id="corr-root",
            workflow_id="wf-root",
            task_id="task-root",
        )
        validator = FactValidator(observability=obs)
        create_calls = []
        original = obs.create_context

        def tracking_create(**kwargs):
            create_calls.append(dict(kwargs))
            return original(**kwargs)

        with patch.object(obs, "create_context", side_effect=tracking_create):
            await validator.validate(
                {"openai": "short"},
                category="strategy",
                parent_context=parent,
            )
        self.assertEqual(create_calls, [])
        events = [
            e for e in obs.list_events() if e.event_type == "validation.completed"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].correlation_id, parent.correlation_id)
        self.assertEqual(events[0].trace_id, parent.trace_id)
        self.assertEqual(events[0].parent_span_id, parent.span_id)

    async def test_5_legacy_without_parent_still_works(self):
        obs = _obs()
        validator = FactValidator(observability=obs)
        result = await validator.validate(
            {"openai": "short"},
            category="strategy",
        )
        self.assertEqual(result.status, "unknown")
        events = [
            e for e in obs.list_events() if e.event_type == "validation.completed"
        ]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].correlation_id)
        self.assertTrue(events[0].trace_id)
        self.assertEqual(events[0].metadata_safe.get("validator_type"), "fact")


class ValidationCorrelationConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_4_concurrent_tenants_no_validation_swap(self):
        obs = _obs()
        validator = FactValidator(observability=obs)
        barrier = asyncio.Barrier(2)

        class BarrierExpert:
            async def run(self, prompt, **kwargs):
                await barrier.wait()
                return {"openai": "strategy text ok"}

        def make_pipeline():
            return Pipeline(
                expert_manager=BarrierExpert(),
                peer_review=_NoopPeer(),
                fact_validator=validator,
                judge=_NoopJudge(),
                response_formatter=_NoopFormatter(),
                supervisor=None,
                decision_memory=_NoopMemory(),
                structural_validator=_Structural(),
                consistency_validator=_Consistency(),
            )

        env_a = _envelope(
            execution_id="exec-a",
            workflow_id="wf-a",
            task_id="task-a",
            tenant_id="tenant-a",
            request_id="req-a",
            correlation_id="corr-a",
            trace_id="trace-a",
            actor_ref="tenant-a:user-a",
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

        # Shared FactValidator, two Pipeline instances overlapping on validate.
        pipe_a = make_pipeline()
        pipe_b = make_pipeline()

        await asyncio.gather(
            pipe_a.execute("prompt-a", envelope=env_a, category="strategy"),
            pipe_b.execute("prompt-b", envelope=env_b, category="strategy"),
        )

        events = [
            e for e in obs.list_events() if e.event_type == "validation.completed"
        ]
        self.assertEqual(len(events), 2)
        by_corr = {e.correlation_id: e for e in events}
        self.assertIn("corr-a", by_corr)
        self.assertIn("corr-b", by_corr)
        self.assertEqual(by_corr["corr-a"].trace_id, "trace-a")
        self.assertEqual(by_corr["corr-a"].workflow_id, "wf-a")
        self.assertEqual(by_corr["corr-a"].task_id, "task-a")
        self.assertEqual(by_corr["corr-a"].metadata_safe.get("tenant_id"), "tenant-a")
        self.assertEqual(by_corr["corr-b"].trace_id, "trace-b")
        self.assertEqual(by_corr["corr-b"].workflow_id, "wf-b")
        self.assertEqual(by_corr["corr-b"].task_id, "task-b")
        self.assertEqual(by_corr["corr-b"].metadata_safe.get("tenant_id"), "tenant-b")


if __name__ == "__main__":
    unittest.main()
