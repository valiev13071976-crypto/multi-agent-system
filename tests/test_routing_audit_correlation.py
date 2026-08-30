"""PATCH-MR-03: provider.selected routing audit correlation to live request identity."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.model_router import ModelRouter, REASON_AUTO_CAPABILITY_MATCH
from agents.provider_registry import ProviderRecord, ProviderRegistry
from agents.model_profile import build_model_profile
from agents.routing_audit import routing_decision_audit_metadata
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime


def _profile(provider_id, model, *, categories="general,technical"):
    return build_model_profile(
        provider_id,
        model,
        task_categories_raw=categories,
        coding_raw="true",
        quality_raw="premium" if provider_id == "openai" else "standard",
        cost_raw="standard",
    )


def _registry(*, available=("openai", "anthropic")):
    profiles = {}
    records = {}
    for pid in ("openai", "anthropic", "gemini", "grok", "deepseek", "moonshot", "mistral"):
        model = "premium" if pid == "openai" else ("cheap" if pid == "anthropic" else f"{pid}-m")
        profiles[pid] = _profile(pid, model)
        records[pid] = ProviderRecord(pid, model, pid in available)
    return ProviderRegistry(
        records,
        profiles=profiles,
        auto_provider_order=("openai", "anthropic"),
        auto_routing_policy="quality",
        auto_capability_fallback="error",
    )


def _obs():
    return ObservabilityRuntime(
        sink=InMemoryObservabilitySink(),
        metrics=MetricsCollector(),
    )


class RoutingAuditCorrelationTests(unittest.TestCase):
    def test_case1_request_correlation(self):
        obs = _obs()
        router = ModelRouter(_registry())
        router.bind_routing_audit(request_id="req-corr-1", observability=obs)
        try:
            router.decide("auto", "technical", category="technical")
        finally:
            router.clear_routing_audit()
        events = [e for e in obs.list_events() if e.event_type == "provider.selected"]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.correlation_id, "req-corr-1")
        self.assertEqual(event.metadata_safe.get("request_id"), "req-corr-1")

    def test_case2_task_correlation(self):
        obs = _obs()
        router = ModelRouter(_registry())
        router.bind_routing_audit(
            request_id="req-2",
            task_id="task-exact-2",
            observability=obs,
        )
        try:
            router.decide("auto", "technical", category="technical")
        finally:
            router.clear_routing_audit()
        event = next(e for e in obs.list_events() if e.event_type == "provider.selected")
        self.assertEqual(event.task_id, "task-exact-2")
        self.assertEqual(event.metadata_safe.get("task_id"), "task-exact-2")

    def test_case3_tenant_correlation(self):
        obs = _obs()
        router = ModelRouter(_registry())
        router.bind_routing_audit(
            request_id="req-3",
            tenant_id="tenant-exact-3",
            observability=obs,
        )
        try:
            router.decide("auto", "technical", category="technical")
        finally:
            router.clear_routing_audit()
        event = next(e for e in obs.list_events() if e.event_type == "provider.selected")
        self.assertEqual(event.metadata_safe.get("tenant_id"), "tenant-exact-3")

    def test_case4_combined_identity(self):
        obs = _obs()
        router = ModelRouter(_registry())
        router.bind_routing_audit(
            request_id="req-4",
            task_id="task-4",
            tenant_id="tenant-4",
            user_id="user-4",
            actor_ref="tenant-4:user-4",
            workflow_id="wf-4",
            observability=obs,
        )
        try:
            router.decide("auto", "technical", category="technical")
        finally:
            router.clear_routing_audit()
        event = next(e for e in obs.list_events() if e.event_type == "provider.selected")
        self.assertEqual(event.correlation_id, "req-4")
        self.assertEqual(event.task_id, "task-4")
        self.assertEqual(event.workflow_id, "wf-4")
        meta = event.metadata_safe
        self.assertEqual(meta.get("request_id"), "req-4")
        self.assertEqual(meta.get("task_id"), "task-4")
        self.assertEqual(meta.get("tenant_id"), "tenant-4")
        self.assertEqual(meta.get("user_id"), "user-4")
        self.assertEqual(meta.get("actor_ref"), "tenant-4:user-4")
        self.assertEqual(meta.get("workflow_id"), "wf-4")

    def test_case5_no_invented_identifiers(self):
        obs = _obs()
        router = ModelRouter(_registry())
        router.bind_routing_audit(request_id="req-only", observability=obs)
        try:
            router.decide("auto", "technical", category="technical")
        finally:
            router.clear_routing_audit()
        event = next(e for e in obs.list_events() if e.event_type == "provider.selected")
        self.assertEqual(event.correlation_id, "req-only")
        self.assertEqual(event.workflow_id, "")
        self.assertEqual(event.task_id, "")
        meta = event.metadata_safe
        self.assertNotIn("tenant_id", meta)  # not invented into identity metadata
        self.assertNotIn("user_id", meta)
        self.assertNotIn("actor_ref", meta)
        self.assertNotIn("workflow_id", meta)
        # Framework still generates trace/span for the event envelope.
        self.assertTrue(event.trace_id)
        self.assertTrue(event.span_id)

    def test_case6_routing_metadata_preserved(self):
        obs = _obs()
        router = ModelRouter(_registry())
        router.bind_routing_audit(request_id="req-6", task_id="t-6", observability=obs)
        try:
            decision = router.decide("auto", "technical", category="technical")
        finally:
            router.clear_routing_audit()
        event = next(e for e in obs.list_events() if e.event_type == "provider.selected")
        meta = event.metadata_safe
        self.assertEqual(meta["route_reason"], decision.reason)
        self.assertEqual(meta["routing_policy_version"], decision.routing_policy_version)
        self.assertIn("candidates_considered", meta)
        self.assertIn("rejected_candidates", meta)
        self.assertIn("rejection_reason_codes", meta)
        self.assertIn("factor_snapshot", meta)
        self.assertEqual(meta["selected_providers"], list(decision.provider_ids))

    def test_case7_routing_result_unchanged(self):
        before = ModelRouter(_registry()).decide(
            "auto", "technical", category="technical"
        )
        obs = _obs()
        router = ModelRouter(_registry())
        router.bind_routing_audit(
            request_id="req-7",
            task_id="task-7",
            tenant_id="tenant-7",
            observability=obs,
        )
        try:
            after = router.decide("auto", "technical", category="technical")
        finally:
            router.clear_routing_audit()
        self.assertEqual(before.provider_ids, after.provider_ids)
        self.assertEqual(before.models, after.models)
        self.assertEqual(before.reason, after.reason)
        self.assertEqual(before.reason, REASON_AUTO_CAPABILITY_MATCH)

    def test_case8_empty_selection_still_correlated(self):
        """No-available-provider path still emits provider.selected with identity."""
        obs = _obs()
        router = ModelRouter(_registry(available=()))
        router.bind_routing_audit(
            request_id="req-empty",
            task_id="task-empty",
            tenant_id="tenant-empty",
            observability=obs,
        )
        try:
            decision = router.decide("auto", "technical", category="technical")
        finally:
            router.clear_routing_audit()
        self.assertEqual(decision.provider_ids, ())
        events = [e for e in obs.list_events() if e.event_type == "provider.selected"]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.correlation_id, "req-empty")
        self.assertEqual(event.task_id, "task-empty")
        self.assertEqual(event.status, "none")
        self.assertEqual(event.metadata_safe.get("request_id"), "req-empty")
        self.assertEqual(event.metadata_safe.get("tenant_id"), "tenant-empty")

    def test_router_v2_binds_identity_into_model_router(self):
        from agents.router_v2 import RouterV2

        obs = _obs()
        captured = {}

        class CaptureRouter(ModelRouter):
            def decide(self, *args, **kwargs):
                captured["request_id"] = self._audit_request_id
                captured["task_id"] = self._audit_task_id
                captured["tenant_id"] = self._audit_tenant_id
                captured["user_id"] = self._audit_user_id
                captured["actor_ref"] = self._audit_actor_ref
                captured["workflow_id"] = self._audit_workflow_id
                captured["observability"] = self.observability
                return super().decide(*args, **kwargs)

        class FakeLifecycle:
            workflow_id = "wf-live"

            async def begin(self, step):
                return True

            async def end(self, step, metadata=None):
                return None

            async def fail(self, step, code):
                return None

        v2 = RouterV2.__new__(RouterV2)
        v2.provider_registry = _registry()
        v2.model_router = CaptureRouter(v2.provider_registry)
        v2.budget_guard = None
        v2.finops = None
        v2.task_classifier = MagicMock()
        v2.health_tracker = None
        v2.runtime_stats = None
        v2.provider_governor = None
        v2.last_task_id = None
        v2.last_workflow_id = None
        v2.last_request_id = None
        v2.last_tenant_id = None
        v2.last_user_id = None
        v2.last_actor_ref = None
        v2.last_decision = None
        v2.last_classification = None
        v2.last_requirements = None
        v2.last_route_context = None
        v2.workflow_engine = MagicMock()
        v2.workflow_engine.observability = obs
        v2.pipeline = MagicMock()
        v2.pipeline.expert_manager = MagicMock()
        v2.pipeline.expert_manager.observability = None
        v2.pipeline.expert_manager.get_provider = MagicMock(return_value=MagicMock())
        v2.pipeline.execute = AsyncMock(return_value={"answer": "ok"})

        async def _run():
            with patch("agents.router_v2.compose_prompt", return_value="composed"):
                with patch("agents.router_v2.get_role_prompt", return_value="role"):
                    return await v2.run(
                        "hello",
                        mode="openai",
                        role="technical",
                        task_id="task-live",
                        lifecycle=FakeLifecycle(),
                        request_id="req-live",
                        tenant_id="tenant-live",
                        user_id="user-live",
                        actor_ref="tenant-live:user-live",
                    )

        import asyncio

        asyncio.run(_run())
        self.assertEqual(captured["request_id"], "req-live")
        self.assertEqual(captured["task_id"], "task-live")
        self.assertEqual(captured["tenant_id"], "tenant-live")
        self.assertEqual(captured["user_id"], "user-live")
        self.assertEqual(captured["actor_ref"], "tenant-live:user-live")
        self.assertEqual(captured["workflow_id"], "wf-live")
        self.assertIs(captured["observability"], obs)
        # Cleared after decide
        self.assertIsNone(v2.model_router._audit_request_id)
        events = [e for e in obs.list_events() if e.event_type == "provider.selected"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].correlation_id, "req-live")
        self.assertEqual(events[0].metadata_safe.get("user_id"), "user-live")
        self.assertEqual(events[0].workflow_id, "wf-live")


if __name__ == "__main__":
    unittest.main()
