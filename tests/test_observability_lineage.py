import unittest
import uuid
from unittest import mock

from autonomy.capabilities import CAP_EXTERNAL_READ, CAP_EXTERNAL_WRITE, CapabilitySet
from autonomy.models import utc_now
from hitl.authority import InMemoryApprovalAuthority, ROLE_PRIVILEGED_APPROVER
from hitl.service import HITLService
from observability.events import InMemoryObservabilitySink
from observability.metrics import MetricsCollector
from observability.runtime import ObservabilityRuntime
from side_effects.executor import SideEffectExecutor
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tests.side_effect_fixtures import T0, caps, eval_kwargs
from tools.adapters import descriptor_from_side_effect
from tools.gateway import ToolGateway
from tools.models import (
    TOOL_TRUST_INTERNAL_SAFE,
    ToolRequest,
)
from tools.registry import ToolRegistry
from tools.search.fake_provider import FakeSearchProvider
from side_effects.models import default_test_descriptor, TEST_TOOL_ID
from workflow.engine import WorkflowEngine


class ObservabilityLineageTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_lineage(self):
        obs = ObservabilityRuntime(
            sink=InMemoryObservabilitySink(), metrics=MetricsCollector()
        )
        engine = WorkflowEngine(observability=obs)
        workflow_id = engine.create("lineage-task")
        engine.state_manager.plan(workflow_id)
        engine.state_manager.start(workflow_id)
        root = obs.context_for_workflow(workflow_id)

        # Read tool under same workflow context
        gateway = ToolGateway(FakeSearchProvider(), observability=obs, obs_context=root)
        await gateway.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                task_id="lineage-task",
                tool_id="search",
                operation="search",
                arguments={"query": "q"},
                requested_capabilities=(CAP_EXTERNAL_READ,),
            ),
            capabilities=CapabilitySet(
                subject_id="a",
                capabilities=(CAP_EXTERNAL_READ,),
                issued_at=utc_now(),
            ),
        )

        # Write path through gate + side effect
        adapter = InMemoryReversibleWriteAdapter(trust_level=TOOL_TRUST_INTERNAL_SAFE)
        se_reg = SideEffectAdapterRegistry()
        se_reg.register(adapter)
        gate = engine._gate()
        gate.observability = obs
        gate.obs_context = root
        executor = SideEffectExecutor(se_reg, gate=gate)
        executor.observability = obs
        registry = ToolRegistry()
        registry.register(
            descriptor_from_side_effect(
                default_test_descriptor(trust_level=TOOL_TRUST_INTERNAL_SAFE),
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=adapter,
        )
        write_gw = ToolGateway(
            registry=registry,
            side_effect_executor=executor,
            gate=gate,
            observability=obs,
            obs_context=root,
            register_search=False,
        )
        await write_gw.invoke(
            ToolRequest(
                request_id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                task_id="lineage-task",
                tool_id=TEST_TOOL_ID,
                operation="set_value",
                arguments={"resource": "test/key", "value": "v"},
                requested_capabilities=(CAP_EXTERNAL_WRITE,),
                idempotency_key="lineage-1",
            ),
            capabilities=caps(CAP_EXTERNAL_WRITE),
            gate=gate,
            executor=executor,
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs(),
            now=T0,
        )

        events = [e for e in obs.list_events() if e.workflow_id == workflow_id]
        self.assertTrue(events)
        corr = {e.correlation_id for e in events}
        traces = {e.trace_id for e in events}
        self.assertEqual(len(corr), 1)
        self.assertEqual(len(traces), 1)
        # parent-child span tree: every non-root has parent in set or equals root
        spans = {e.span_id for e in events}
        for e in events:
            if e.parent_span_id is not None:
                self.assertTrue(
                    e.parent_span_id in spans or e.parent_span_id == root.span_id
                )
        blob = str([(e.event_type, dict(e.metadata_safe)) for e in events])
        for needle in ("Bearer ", "GITHUB_WRITE_TOKEN", "PANDA_ENCRYPTION_KEY"):
            self.assertNotIn(needle, blob)
        _ = ROLE_PRIVILEGED_APPROVER
        _ = HITLService
        _ = mock
        _ = InMemoryApprovalAuthority


if __name__ == "__main__":
    unittest.main()
