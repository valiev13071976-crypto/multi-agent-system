"""P1-OBS Block 3 — ProviderGovernor live-path correlation."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from observability.context import ObservabilityContext
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime
from providers.governor import (
    GovernorLimits,
    InMemoryProviderGovernorStore,
    ProviderGovernor,
)
from workflow.run_envelope import RunEnvelope


def _obs():
    return ObservabilityRuntime(
        sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
    )


def _envelope(**overrides) -> RunEnvelope:
    base = dict(
        workflow_id="wf-gov-1",
        task_id="task-gov-1",
        tenant_id="tenant-gov-1",
        request_id="req-gov-1",
        correlation_id="corr-gov-1",
        trace_id="trace-gov-1",
        user_id="user-gov-1",
        actor_ref="tenant-gov-1:user-gov-1",
        execution_id="exec-gov-1",
        created_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return RunEnvelope.create(**base)


def _gov(obs=None):
    limits = GovernorLimits(enabled=True)
    return ProviderGovernor(
        store=InMemoryProviderGovernorStore(limits),
        limits=limits,
        observability=obs,
    )


class ProviderGovernorCorrelationTests(unittest.TestCase):
    def test_parent_correlation_preserved_on_acquire(self):
        obs = _obs()
        parent = ObservabilityContext.root(
            correlation_id="corr-parent",
            workflow_id="wf-p",
            task_id="task-p",
            tenant_id="tenant-p",
            actor_ref="actor-p",
        )
        gov = _gov(obs)
        slot = gov.acquire(
            provider_id="openai",
            model_id="m",
            lane="interactive",
            parent_context=parent,
            workflow_id="wf-p",
            task_id="task-p",
            tenant_id="tenant-p",
            actor_ref="actor-p",
        )
        gov.release(
            slot,
            parent_context=parent,
            workflow_id="wf-p",
            task_id="task-p",
            tenant_id="tenant-p",
            actor_ref="actor-p",
        )
        events = [
            e
            for e in obs.list_events()
            if e.event_type.startswith("provider.")
        ]
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event.correlation_id, "corr-parent")
            self.assertEqual(event.trace_id, parent.trace_id)
            self.assertEqual(event.parent_span_id, parent.span_id)

    def test_envelope_lineage_no_independent_root(self):
        obs = _obs()
        parent = ObservabilityContext.root(
            correlation_id="corr-env",
            workflow_id="wf-gov-1",
            task_id="task-gov-1",
            tenant_id="tenant-gov-1",
            actor_ref="tenant-gov-1:user-gov-1",
        )
        obs.bind_workflow_context(parent.workflow_id, parent)
        envelope = _envelope(trace_id=parent.trace_id, correlation_id="corr-env")
        gov = _gov(obs)
        create_calls = []
        original = obs.create_context

        def tracking_create(**kwargs):
            create_calls.append(dict(kwargs))
            return original(**kwargs)

        with patch.object(obs, "create_context", side_effect=tracking_create):
            slot = gov.acquire(
                provider_id="openai",
                model_id="m",
                lane="interactive",
                envelope=envelope,
            )
            gov.release(slot, envelope=envelope)
        self.assertEqual(create_calls, [])
        events = [
            e
            for e in obs.list_events()
            if e.event_type.startswith("provider.")
        ]
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event.correlation_id, "corr-env")
            self.assertEqual(event.trace_id, parent.trace_id)
            self.assertEqual(event.workflow_id, "wf-gov-1")
            self.assertEqual(event.task_id, "task-gov-1")
            self.assertEqual(event.metadata_safe.get("tenant_id"), "tenant-gov-1")

    def test_legacy_without_parent_still_works(self):
        obs = _obs()
        gov = _gov(obs)
        slot = gov.acquire(provider_id="openai", model_id="m", lane="interactive")
        gov.release(slot)
        types = {e.event_type for e in obs.list_events()}
        self.assertIn("provider.capacity_acquired", types)
        self.assertIn("provider.capacity_released", types)


if __name__ == "__main__":
    unittest.main()
