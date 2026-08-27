"""Commerce workflow definitions — same WorkflowRuntime / TaskQueue."""

from __future__ import annotations

from workflow.definition import (
    STEP_TYPE_HANDLER,
    StepResult,
    WorkflowDefinition,
    WorkflowStep,
)


def _svc(ctx):
    platform = ctx["platform"]
    engine = getattr(platform, "workflow_engine", None)
    return getattr(engine, "commerce_service", None) if engine else None


def procurement_receive_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="commerce.procurement_receive",
        version="1",
        timeout_seconds=600.0,
        steps=(WorkflowStep(step_id="commerce_procurement_run", step_type=STEP_TYPE_HANDLER),),
    )


def b2c_fulfillment_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="commerce.b2c_fulfillment",
        version="1",
        timeout_seconds=1800.0,
        steps=(WorkflowStep(step_id="commerce_b2c_run", step_type=STEP_TYPE_HANDLER),),
    )


def b2b_own_use_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="commerce.b2b_own_use",
        version="1",
        timeout_seconds=1800.0,
        steps=(WorkflowStep(step_id="commerce_b2b_own_run", step_type=STEP_TYPE_HANDLER),),
    )


def b2b_resale_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="commerce.b2b_resale",
        version="1",
        timeout_seconds=1800.0,
        steps=(WorkflowStep(step_id="commerce_b2b_resale_run", step_type=STEP_TYPE_HANDLER),),
    )


def return_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="commerce.return",
        version="1",
        timeout_seconds=600.0,
        steps=(WorkflowStep(step_id="commerce_return_run", step_type=STEP_TYPE_HANDLER),),
    )


def cancel_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="commerce.cancel",
        version="1",
        timeout_seconds=300.0,
        steps=(WorkflowStep(step_id="commerce_cancel_run", step_type=STEP_TYPE_HANDLER),),
    )


def reconcile_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="commerce.reconcile",
        version="1",
        timeout_seconds=300.0,
        steps=(WorkflowStep(step_id="commerce_reconcile_run", step_type=STEP_TYPE_HANDLER),),
    )


async def commerce_workflow_handler(ctx) -> StepResult:
    step = ctx["step"]
    state = ctx["state"]
    meta = dict(state.metadata or {})
    tenant_id = str(meta.get("tenant_id") or "legacy-default")
    order_id = str(meta.get("order_id") or "")
    caps = tuple(meta.get("capabilities") or ())
    svc = _svc(ctx)
    if svc is None:
        return StepResult(ok=False, data={"error": "commerce_unavailable"})

    if step.step_id == "commerce_procurement_run":
        result = svc.procurement_receive(
            tenant_id=tenant_id,
            supplier_id=str(meta.get("supplier_id") or ""),
            lines=list(meta.get("lines") or []),
            expected_lines=list(meta.get("expected_lines") or []),
            idempotency_key=str(meta.get("idempotency_key") or state.workflow_id),
            capabilities=caps,
        )
        return StepResult(ok=result.get("status") != "NEEDS_REVIEW", data=result)

    if step.step_id == "commerce_b2c_run":
        result = svc.run_b2c_fulfillment(
            tenant_id,
            order_id,
            capabilities=caps,
            idempotency_key=str(meta.get("idempotency_key") or state.workflow_id),
            fiscal_ok=bool(meta.get("fiscal_ok", True)),
            marking_ok=bool(meta.get("marking_ok", True)),
        )
        return StepResult(ok=result.status == "completed", data={"status": result.status, "error": result.error})

    if step.step_id == "commerce_b2b_own_run":
        result = svc.run_b2b_own_use(
            tenant_id,
            order_id,
            capabilities=caps,
            idempotency_key=str(meta.get("idempotency_key") or state.workflow_id),
        )
        return StepResult(ok=result.status == "completed", data={"status": result.status, "error": result.error})

    if step.step_id == "commerce_b2b_resale_run":
        result = svc.run_b2b_resale(
            tenant_id,
            order_id,
            capabilities=caps,
            idempotency_key=str(meta.get("idempotency_key") or state.workflow_id),
        )
        return StepResult(ok=result.status == "completed", data={"status": result.status, "error": result.error})

    if step.step_id == "commerce_return_run":
        result = svc.return_order(
            tenant_id,
            order_id,
            capabilities=caps,
            reintroduce_marking=bool(meta.get("reintroduce_marking")),
            hitl_approved=bool(meta.get("hitl_approved")),
            idempotency_key=str(meta.get("idempotency_key") or state.workflow_id),
        )
        return StepResult(ok=result.status == "completed", data={"status": result.status})

    if step.step_id == "commerce_cancel_run":
        result = svc.cancel_order(tenant_id, order_id, capabilities=caps)
        return StepResult(ok=True, data={"status": result.status})

    if step.step_id == "commerce_reconcile_run":
        result = svc.reconcile_order(tenant_id, order_id)
        return StepResult(ok=result.get("severity") == "OK", data=result)

    return StepResult(ok=True, data={"step_id": step.step_id})


def register_commerce_workflows(definitions, platform) -> None:
    for factory in (
        procurement_receive_definition,
        b2c_fulfillment_definition,
        b2b_own_use_definition,
        b2b_resale_definition,
        return_definition,
        cancel_definition,
        reconcile_definition,
    ):
        try:
            definitions.register(factory())
        except Exception:
            pass
    for step_id in (
        "commerce_procurement_run",
        "commerce_b2c_run",
        "commerce_b2b_own_run",
        "commerce_b2b_resale_run",
        "commerce_return_run",
        "commerce_cancel_run",
        "commerce_reconcile_run",
    ):
        platform.register_handler(step_id, commerce_workflow_handler)
