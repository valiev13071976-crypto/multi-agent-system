"""Production Runtime / Scale — 3.12 Interactive vs Batch/Background Workload.

Acceptance: Business Assistant conversational traffic (a live user waiting
synchronously for a reply) must be classified WORKLOAD_INTERACTIVE, and that
classification must propagate end-to-end -- RunEnvelope -> UnifiedToolExecutor
-> ToolGateway routing/audit -- as TRUSTED, server-set context that cannot be
spoofed via user-controlled request content (tool arguments / message text).

Prior state (verified in task_queue/lanes.py, tools/router.py,
business_assistant_api/service.py): the WorkloadClass taxonomy and
classify_workload() rules already existed, and ToolRouter already had
workload_class-aware routing/rejection logic -- but nothing actually threaded
a real caller's workload_class into it:

- RunEnvelope had no `workload_class` field at all.
- WorkflowEngine.execute() had no `workload_class` parameter.
- WorkflowPandaConversationGateway never passed workload_class to
  WorkflowEngine.execute() (fallback conversational path) nor set it on the
  ToolRequest.metadata it builds for the CALL_TOOL path (e.g. image.generate).
- ToolGateway.invoke() never read request.metadata["workload_class"] nor
  passed it to ToolRouter.route(), so the router's own workload-aware checks
  always ran with workload_class=None for every real caller.

Fix (in-scope for 3.12, implemented here):
- workflow/run_envelope.py: RunEnvelope gained a normalized `workload_class`
  field (create/as_dict/from_dict).
- workflow/engine.py: WorkflowEngine.execute() accepts `workload_class` and
  stamps it onto the RunEnvelope it creates.
- business_assistant/conversation_gateway.py: WorkflowPandaConversationGateway
  now passes workload_class=WORKLOAD_INTERACTIVE (a hardcoded server
  constant, never derived from request.text) both to WorkflowEngine.execute()
  and as ToolRequest.metadata for the CALL_TOOL/ToolGateway path.
- tools/executor.py: UnifiedToolExecutor.build_trusted_request() propagates
  envelope.workload_class into ToolRequest.metadata["workload_class"],
  winning over any conflicting metadata argument (RunEnvelope is the trusted
  identity source of truth).
- tools/gateway.py: ToolGateway.invoke() reads workload_class only from
  request.metadata (never request.arguments) and passes it to
  ToolRouter.route(...), and records it on the tool.requested /
  EVENT_TOOL_REQUESTED / EVENT_TOOL_ROUTED audit trail for observability.

Persistence/retry/resume/reclaim (task_queue/queue.py) already preserved
execution_lane/workload metadata correctly before this block (verified by
inspection of `_transition`/`redrive_dead_letter`, which never resets
`execution_lane` or `metadata`); a regression assertion is added below to
lock that in explicitly for this acceptance area.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from workflow.run_envelope import RunEnvelope


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


class RunEnvelopeWorkloadClassTests(unittest.TestCase):
    def test_workload_class_normalizes_and_defaults_empty(self):
        default = _sample_envelope()
        self.assertEqual(default.workload_class, "")

        ix = _sample_envelope(workload_class="interactive")
        self.assertEqual(ix.workload_class, "interactive")

        alias = _sample_envelope(workload_class="foreground")
        self.assertEqual(alias.workload_class, "interactive")

        bulk = _sample_envelope(workload_class="bulk")
        self.assertEqual(bulk.workload_class, "batch")

    def test_workload_class_round_trips_as_dict_from_dict(self):
        original = _sample_envelope(workload_class="interactive")
        restored = RunEnvelope.from_dict(original.as_dict())
        self.assertEqual(restored.workload_class, "interactive")
        self.assertEqual(restored.as_dict(), original.as_dict())


class WorkflowEngineInteractiveClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_stamps_interactive_workload_on_envelope(self):
        from observability.runtime import build_observability_runtime
        from workflow.engine import WorkflowEngine

        obs = build_observability_runtime(env={})
        engine = WorkflowEngine(observability=obs)

        class CM:
            async def prepare(self, prompt, **kwargs):
                return prompt

        async def run_router(**kwargs):
            return {"final_answer": "ok", "envelope_workload_class": kwargs["envelope"].workload_class}

        result = await engine.execute(
            "hello",
            "auto",
            "auto",
            context_manager=CM(),
            run_router=run_router,
            task_id="task-ix",
            tenant_id="tenant-ix",
            request_id="req-ix",
            user_id="user-ix",
            actor_ref="tenant-ix:user-ix",
            workload_class="interactive",
        )
        self.assertEqual(engine.last_run_envelope.workload_class, "interactive")
        self.assertEqual(result["envelope_workload_class"], "interactive")

    async def test_execute_defaults_to_unclassified_when_omitted(self):
        from observability.runtime import build_observability_runtime
        from workflow.engine import WorkflowEngine

        obs = build_observability_runtime(env={})
        engine = WorkflowEngine(observability=obs)

        class CM:
            async def prepare(self, prompt, **kwargs):
                return prompt

        async def run_router(**kwargs):
            return {"final_answer": "ok"}

        await engine.execute(
            "hello",
            "auto",
            "auto",
            context_manager=CM(),
            run_router=run_router,
            task_id="task-none",
            tenant_id="tenant-none",
            request_id="req-none",
        )
        self.assertEqual(engine.last_run_envelope.workload_class, "")


class UnifiedToolExecutorPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_envelope_workload_class_wins_over_conflicting_metadata(self):
        from tools.executor import UnifiedToolExecutor
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        executor = UnifiedToolExecutor(registry=registry, gateway=object())
        envelope = _sample_envelope(workload_class="interactive")

        request = executor.build_trusted_request(
            tool_id="image.generate",
            operation="generate",
            arguments={"scene_description": "a red car"},
            envelope=envelope,
            # Caller-supplied metadata attempting a different class -- envelope
            # (trusted identity source of truth) must win.
            metadata={"workload_class": "batch"},
        )
        self.assertEqual(request.metadata.get("workload_class"), "interactive")
        self.assertEqual(request.tenant_id, "tenant-1")
        self.assertEqual(request.envelope_ref, "exec-1")

    async def test_no_envelope_keeps_explicit_metadata(self):
        from tools.executor import UnifiedToolExecutor
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        executor = UnifiedToolExecutor(registry=registry, gateway=object())
        request = executor.build_trusted_request(
            tool_id="image.generate",
            operation="generate",
            arguments={},
            envelope=None,
            metadata={"workload_class": "batch"},
        )
        self.assertEqual(request.metadata.get("workload_class"), "batch")


class ToolGatewayWorkloadPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_reads_workload_class_from_metadata_not_arguments(self):
        """Trust boundary: workload_class must come from ToolRequest.metadata
        (server-set trusted context) -- a value smuggled into `arguments`
        (user-controlled tool call payload) must be ignored entirely."""
        from side_effects.runtime import compose_side_effect_runtime
        from tools.models import ToolRequest

        runtime = compose_side_effect_runtime(env={})
        gateway = runtime.tool_gateway

        request = ToolRequest(
            request_id="r1",
            workflow_id="w1",
            task_id="t1",
            tenant_id="tenant-a",
            user_id="user-a",
            tool_id="excel.inspect",
            operation="inspect",
            arguments={"workload_class": "batch"},  # spoof attempt, must be ignored
            metadata={"workload_class": "interactive"},  # trusted, server-set
        )
        await gateway.invoke(request)

        events = [e for e in gateway.audit.list_all() if e["event_type"] == "tool.requested"]
        self.assertTrue(events, "expected at least one tool.requested audit event")
        self.assertEqual(events[-1]["workload_class"], "interactive")


class BusinessAssistantInteractivePropagationTests(unittest.IsolatedAsyncioTestCase):
    """The concrete 3.12 acceptance: the real Business Assistant -> ToolGateway
    path (used for e.g. image.generate) always carries workload_class
    "interactive" -- server-classified, and immune to a spoof attempt via the
    tool's own argument payload."""

    async def test_red_car_prompt_call_tool_carries_interactive_workload_class(self):
        from unittest.mock import AsyncMock, Mock

        from business_assistant.conversation_gateway import (
            ConversationRequest,
            WorkflowPandaConversationGateway,
        )
        from product_media.providers.fake import FakeImageGenerationProvider
        from side_effects.runtime import compose_side_effect_runtime

        runtime = compose_side_effect_runtime(env={})
        runtime.product_media_service.generator = FakeImageGenerationProvider()
        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "unused"})
        engine.last_workflow_id = "wf-1"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=object(),
            context_manager=object(),
            tool_gateway=runtime.tool_gateway,
        )

        result = await gw.respond(
            ConversationRequest(
                text="Нарисуй красную машину на белом фоне.",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r1",
                conversation_id="c1",
            )
        )
        self.assertEqual(gw.last_action_decision.decision, "CALL_TOOL")
        self.assertIn("Готово", result.text)

        requested = [
            e for e in runtime.tool_gateway.audit.list_all()
            if e["event_type"] == "tool.requested" and e.get("tool_id") == "image.generate"
        ]
        self.assertTrue(requested, "expected image.generate tool.requested audit event")
        self.assertEqual(requested[-1]["workload_class"], "interactive")

        routed = [
            e for e in runtime.tool_gateway.audit.list_all()
            if e["event_type"] == "tool.routed" and e.get("tool_id") == "image.generate"
        ]
        self.assertTrue(routed)
        self.assertEqual(routed[-1]["workload_class"], "interactive")

    async def test_fallback_conversational_path_also_classified_interactive(self):
        """Non-tool conversational replies (e.g. general Q&A) go through
        WorkflowEngine.execute(), which must also receive workload_class
        "interactive" from Business Assistant."""
        from unittest.mock import AsyncMock, Mock

        from business_assistant.conversation_gateway import (
            ConversationRequest,
            WorkflowPandaConversationGateway,
        )

        engine = Mock()
        engine.execute = AsyncMock(return_value={"final_answer": "Panda's answer"})
        engine.last_workflow_id = "wf-2"
        gw = WorkflowPandaConversationGateway(
            workflow_engine=engine,
            run_router=Mock(),
            context_manager=object(),
            tool_gateway=None,
        )

        await gw.respond(
            ConversationRequest(
                text="Расскажи про НДС для ИП",
                tenant_id="tenant-a",
                user_id="user-a",
                request_id="r2",
                conversation_id="c2",
            )
        )
        self.assertTrue(engine.execute.await_args is not None)
        self.assertEqual(engine.execute.await_args.kwargs.get("workload_class"), "interactive")


class DlqRedrivePreservesWorkloadLaneTests(unittest.TestCase):
    """Persistence/retry/resume/reclaim: workload classification (execution
    lane) must survive a dead-letter redrive, not silently reset to a
    default lane."""

    def test_redrive_preserves_execution_lane(self):
        from task_queue.lanes import LANE_BULK
        from task_queue.queue import TaskQueue
        from task_queue.store import InMemoryTaskQueueStore

        q = TaskQueue(store=InMemoryTaskQueueStore())
        q.enqueue(
            workflow_id="wf",
            task_id="t",
            execution_key="ek-3-12-dlq",
            tenant_id="tenant-a",
            execution_lane=LANE_BULK,
            priority="low",
        )
        leased = q.dequeue(worker_id="w0")
        assert leased is not None
        self.assertEqual(leased.execution_lane, LANE_BULK)
        q.start(leased.queue_task_id, leased.lease_id, worker_id="w0")
        lettered = q.dead_letter(
            leased.queue_task_id, leased.lease_id, error_code="permanent_failure", worker_id="w0"
        )
        redriven = q.redrive_dead_letter(
            lettered.queue_task_id, actor_ref="ops", tenant_id="tenant-a"
        )
        self.assertEqual(redriven.execution_lane, LANE_BULK)


if __name__ == "__main__":
    unittest.main()
