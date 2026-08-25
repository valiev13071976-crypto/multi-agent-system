from datetime import datetime, timedelta, timezone

from autonomy.capabilities import CAP_EXTERNAL_WRITE, CapabilitySet
from autonomy.gate import AutonomyGate, build_proposed_action
from hitl.authority import (
    InMemoryApprovalAuthority,
    ROLE_PRIVILEGED_APPROVER,
)
from hitl.service import HITLService
from side_effects.executor import SideEffectExecutor
from side_effects.models import (
    TEST_TOOL_ID,
    SideEffectExecutionContext,
)
from side_effects.registry import SideEffectAdapterRegistry
from side_effects.test_adapter import InMemoryReversibleWriteAdapter
from tools.models import TOOL_TRUST_INTERNAL_SAFE, TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
from workflow.engine import WorkflowEngine


T0 = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def caps(*names):
    return CapabilitySet(subject_id="agent-1", capabilities=tuple(names), issued_at=T0)


def eval_kwargs(level="executor_bounded", capabilities=None):
    names = capabilities
    if names is None:
        names = (CAP_EXTERNAL_WRITE,)
    return {
        "capabilities": caps(*names),
        "autonomy_level": level,
        "now": T0,
    }


def se_action(workflow_id, task_id="task-se", **kwargs):
    trust = kwargs.pop("tool_trust_level", TOOL_TRUST_INTERNAL_SAFE)
    fields = {
        "action_type": "write",
        "workflow_id": workflow_id,
        "task_id": task_id,
        "tool_id": kwargs.pop("tool_id", TEST_TOOL_ID),
        "operation": kwargs.pop("operation", "set_value"),
        "resource": kwargs.pop("resource", "test/key"),
        "idempotency_key": kwargs.pop("idempotency_key", "idem-se"),
        "metadata": kwargs.pop("metadata", {"reversible": True}),
        "tool_trust_level": trust,
        "requested_capabilities": kwargs.pop(
            "requested_capabilities", (CAP_EXTERNAL_WRITE,)
        ),
    }
    if trust == TOOL_TRUST_INTERNAL_SAFE and "risk_class" not in kwargs:
        fields["risk_class"] = "low"
    fields.update(kwargs)
    return build_proposed_action(**fields)


def ctx(value="ok"):
    return SideEffectExecutionContext(payload={"value": value}, now=T0)


def runtime(trust=TOOL_TRUST_INTERNAL_SAFE, reversible=True):
    engine = WorkflowEngine()
    workflow_id = engine.create("task-se")
    engine.state_manager.plan(workflow_id)
    engine.state_manager.start(workflow_id)
    adapter = InMemoryReversibleWriteAdapter(trust_level=trust, reversible=reversible)
    registry = SideEffectAdapterRegistry()
    registry.register(adapter)
    executor = SideEffectExecutor(registry, gate=engine._gate())
    engine.side_effect_executor = executor
    return engine, workflow_id, adapter, executor


def hitl_runtime(trust=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE, **runtime_kwargs):
    engine, workflow_id, adapter, executor = runtime(trust=trust, **runtime_kwargs)
    authority = InMemoryApprovalAuthority()
    authority.grant("reviewer-1", ROLE_PRIVILEGED_APPROVER)
    engine.hitl_service = HITLService(
        gate=engine._gate(),
        state_manager=engine.state_manager,
        store=engine._gate().approvals.store,
        authority=authority,
        approval_ttl_seconds=3600,
        permit_ttl_seconds=300,
    )
    return engine, workflow_id, adapter, executor


async def allow_execute(executor, action, engine, adapter_value="ok", **extra):
    kwargs = eval_kwargs()
    decision = engine._gate().evaluate(action, **kwargs)
    return await executor.execute(
        action,
        decision=decision,
        context=ctx(adapter_value),
        gate=engine._gate(),
        state_manager=engine.state_manager,
        evaluate_kwargs=kwargs,
        **extra,
    )


async def issue_permit(engine, action, level="executor_confirmed"):
    kwargs = eval_kwargs(level, capabilities=action.requested_capabilities)
    engine.evaluate_action(action, requested_by="agent-1", **kwargs)
    engine._hitl().approve(engine.last_approval_id, resolved_by="reviewer-1", now=T0)
    permit = engine._hitl().reevaluate_and_issue_permit(
        engine.last_approval_id,
        action,
        **kwargs,
    )
    return permit
