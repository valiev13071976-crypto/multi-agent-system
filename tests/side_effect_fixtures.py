from datetime import datetime, timedelta, timezone

from autonomy.capabilities import CAP_EXTERNAL_WRITE, CAP_GITHUB_ISSUE_LABEL_WRITE, CapabilitySet
from autonomy.gate import AutonomyGate, build_proposed_action
from hitl.authority import (
    InMemoryApprovalAuthority,
    ROLE_PRIVILEGED_APPROVER,
)
from hitl.service import HITLService
from side_effects.github.adapter import GitHubIssueLabelAdapter
from side_effects.github.activation import GitHubWriteActivationService
from side_effects.github.config import GitHubWriteAdapterConfig
from side_effects.github.models import GITHUB_TOOL_ID, OP_ENSURE_PRESENT
from side_effects.github.transport import FakeGitHubTransport
from side_effects.reconciliation import SideEffectReconciliationService
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


def runtime(trust=TOOL_TRUST_INTERNAL_SAFE, reversible=True, *, tenant_id="tenant-se"):
    engine = WorkflowEngine()
    workflow_id = engine.create("task-se", tenant_id=tenant_id)
    engine.state_manager.plan(workflow_id)
    engine.state_manager.start(workflow_id)
    adapter = InMemoryReversibleWriteAdapter(trust_level=trust, reversible=reversible)
    registry = SideEffectAdapterRegistry()
    registry.register(adapter)
    executor = SideEffectExecutor(registry, gate=engine._gate())
    engine.side_effect_executor = executor
    return engine, workflow_id, adapter, executor


def recon_runtime(trust=TOOL_TRUST_INTERNAL_SAFE, reversible=True, **service_kwargs):
    engine, workflow_id, adapter, executor = runtime(trust=trust, reversible=reversible)
    service = SideEffectReconciliationService(
        execution_store=executor.store,
        idempotency=engine._gate().idempotency,
        registry=executor.registry,
        audit=executor.audit,
        state_manager=engine.state_manager,
        **service_kwargs,
    )
    executor.reconciliation_service = service
    engine.reconciliation_service = service
    return engine, workflow_id, adapter, executor, service


async def make_uncertain(executor, action, engine, value="mutated"):
    kwargs = eval_kwargs()
    decision = engine._gate().evaluate(action, **kwargs)
    context = ctx(value)
    context.simulate_finalization_failure = True
    return await executor.execute(
        action,
        decision=decision,
        context=context,
        gate=engine._gate(),
        state_manager=engine.state_manager,
        evaluate_kwargs=kwargs,
    )


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


GITHUB_CAPS = (CAP_EXTERNAL_WRITE, CAP_GITHUB_ISSUE_LABEL_WRITE)
GITHUB_RESOURCE = "github://octo/hello/issues/1/labels/bug"


def github_eval_kwargs(level="executor_confirmed", capabilities=None):
    names = capabilities if capabilities is not None else GITHUB_CAPS
    return eval_kwargs(level, capabilities=names)


def github_action(workflow_id, task_id="task-se", **kwargs):
    return se_action(
        workflow_id,
        task_id=task_id,
        tool_id=kwargs.pop("tool_id", GITHUB_TOOL_ID),
        operation=kwargs.pop("operation", OP_ENSURE_PRESENT),
        resource=kwargs.pop("resource", GITHUB_RESOURCE),
        tool_trust_level=kwargs.pop(
            "tool_trust_level", TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE
        ),
        requested_capabilities=kwargs.pop("requested_capabilities", GITHUB_CAPS),
        metadata=kwargs.pop("metadata", {"reversible": True}),
        **kwargs,
    )


def github_runtime(
    *,
    config=None,
    transport=None,
    timeout_seconds=15.0,
    tenant_id="tenant-se",
):
    engine = WorkflowEngine()
    workflow_id = engine.create("task-se", tenant_id=tenant_id)
    engine.state_manager.plan(workflow_id)
    engine.state_manager.start(workflow_id)
    fake = transport or FakeGitHubTransport()
    cfg = config or GitHubWriteAdapterConfig(
        enabled=True,
        allowed_repositories=("octo/hello",),
        timeout_seconds=timeout_seconds,
        dry_run=False,
        kill_switch=False,
        require_probe_success=False,
    )
    adapter = GitHubIssueLabelAdapter(config=cfg, transport=fake)
    registry = SideEffectAdapterRegistry()
    registry.register(adapter)
    executor = SideEffectExecutor(registry, gate=engine._gate())
    engine.side_effect_executor = executor
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
    return engine, workflow_id, adapter, executor, fake


def github_activation_runtime(*, config=None, timeout_seconds=15.0, transport=None):
    engine, workflow_id, adapter, executor, fake = github_runtime(
        config=config, timeout_seconds=timeout_seconds, transport=transport
    )
    service = GitHubWriteActivationService(
        config=adapter._config,
        transport=fake,
        audit=executor.audit,
        registered=True,
    )
    executor.activation = service
    return engine, workflow_id, adapter, executor, fake, service


def github_recon_runtime(**kwargs):
    recon_timeout = kwargs.pop("recon_timeout", 0.05)
    engine, workflow_id, adapter, executor, fake = github_runtime(**kwargs)
    service = SideEffectReconciliationService(
        execution_store=executor.store,
        idempotency=engine._gate().idempotency,
        registry=executor.registry,
        audit=executor.audit,
        state_manager=engine.state_manager,
        timeout_seconds=recon_timeout,
    )
    executor.reconciliation_service = service
    engine.reconciliation_service = service
    return engine, workflow_id, adapter, executor, fake, service


async def github_execute(executor, action, engine, context=None, **extra):
    permit = await issue_permit(engine, action)
    ctx_obj = context if context is not None else SideEffectExecutionContext(now=T0)
    return await executor.execute(
        action,
        permit=permit,
        context=ctx_obj,
        gate=engine._gate(),
        hitl=engine._hitl(),
        state_manager=engine.state_manager,
        evaluate_kwargs=github_eval_kwargs(),
        **extra,
    )

