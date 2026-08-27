"""Payments workflow definitions — shared WorkflowRuntime / TaskQueue."""

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
    return getattr(engine, "payments_service", None) if engine else None


def process_event_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="payments.process_event",
        version="1",
        timeout_seconds=300.0,
        steps=(WorkflowStep(step_id="payments_process_event_run", step_type=STEP_TYPE_HANDLER),),
    )


def ingest_statement_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="payments.ingest_statement",
        version="1",
        timeout_seconds=600.0,
        steps=(WorkflowStep(step_id="payments_ingest_statement_run", step_type=STEP_TYPE_HANDLER),),
    )


def match_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="payments.match",
        version="1",
        timeout_seconds=300.0,
        steps=(WorkflowStep(step_id="payments_match_run", step_type=STEP_TYPE_HANDLER),),
    )


def allocate_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="payments.allocate",
        version="1",
        timeout_seconds=300.0,
        steps=(WorkflowStep(step_id="payments_allocate_run", step_type=STEP_TYPE_HANDLER),),
    )


def reconcile_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="payments.reconcile",
        version="1",
        timeout_seconds=600.0,
        steps=(WorkflowStep(step_id="payments_reconcile_run", step_type=STEP_TYPE_HANDLER),),
    )


def prepare_refund_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="payments.prepare_refund",
        version="1",
        timeout_seconds=300.0,
        steps=(WorkflowStep(step_id="payments_prepare_refund_run", step_type=STEP_TYPE_HANDLER),),
    )


def execute_refund_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="payments.execute_refund",
        version="1",
        timeout_seconds=300.0,
        steps=(WorkflowStep(step_id="payments_execute_refund_run", step_type=STEP_TYPE_HANDLER),),
    )


async def payments_workflow_handler(ctx) -> StepResult:
    step = ctx["step"]
    state = ctx["state"]
    meta = dict(state.metadata or {})
    tenant_id = str(meta.get("tenant_id") or "legacy-default")
    svc = _svc(ctx)
    if svc is None:
        return StepResult(ok=False, data={"error": "payments_unavailable"})

    workflow_id = getattr(state, "workflow_id", "") or ""
    run_id = str(
        getattr(state, "execution_key", None) or meta.get("run_id") or workflow_id or ""
    )
    caps = tuple(meta.get("capabilities") or ())

    if step.step_id == "payments_process_event_run":
        payment_id = str(meta.get("payment_id") or "")
        if payment_id:
            match = svc.match_payment(tenant_id, payment_id)
            if match.selected_order_id and not match.review_required:
                svc.allocate(
                    tenant_id,
                    payment_id,
                    match.selected_order_id,
                    float(meta.get("amount") or 0)
                    or float(svc._get_payment(tenant_id, payment_id).amount),
                    invoice_id=match.selected_invoice_id,
                    method="auto_match",
                    capabilities=caps or ("payments.allocate",),
                    confidence=match.confidence,
                )
                svc.apply_unlock_to_commerce(tenant_id, match.selected_order_id)
            return StepResult(ok=True, data={"match_id": match.match_id, "status": match.status})
        return StepResult(ok=True, data={"status": "noop"})

    if step.step_id == "payments_ingest_statement_run":
        rows = list(meta.get("rows") or [])
        result = svc.ingest_statement_rows(
            tenant_id,
            rows,
            account_ref=str(meta.get("account_ref") or "main"),
            statement_ref=str(meta.get("statement_ref") or run_id),
            period_start=str(meta.get("period_start") or ""),
            period_end=str(meta.get("period_end") or ""),
        )
        return StepResult(ok=True, data=result)

    if step.step_id == "payments_match_run":
        payment_id = str(meta.get("payment_id") or "")
        tx_id = str(meta.get("transaction_id") or "")
        if payment_id:
            result = svc.match_payment(tenant_id, payment_id)
        elif tx_id:
            result = svc.match_bank_tx(tenant_id, tx_id)
        else:
            return StepResult(ok=False, data={"error": "match_target_required"})
        return StepResult(
            ok=True,
            data={
                "match_id": result.match_id,
                "status": result.status,
                "review_required": result.review_required,
                "confidence": result.confidence,
            },
        )

    if step.step_id == "payments_allocate_run":
        result = svc.allocate(
            tenant_id,
            str(meta.get("payment_id") or ""),
            str(meta.get("order_id") or ""),
            float(meta.get("amount") or 0),
            invoice_id=str(meta.get("invoice_id") or ""),
            method=str(meta.get("method") or "workflow"),
            capabilities=caps or ("payments.allocate",),
            idempotency_key=str(meta.get("idempotency_key") or run_id),
        )
        return StepResult(ok=True, data={"allocation_id": result.allocation_id})

    if step.step_id == "payments_reconcile_run":
        result = svc.reconcile_tenant(
            tenant_id, workflow_id=workflow_id, run_id=run_id
        )
        return StepResult(ok=True, data=result)

    if step.step_id == "payments_prepare_refund_run":
        result = svc.prepare_refund(
            tenant_id,
            payment_id=str(meta.get("payment_id") or ""),
            amount=float(meta.get("amount") or 0),
            reason=str(meta.get("reason") or ""),
            order_id=str(meta.get("order_id") or ""),
            capabilities=caps or ("payments.prepare_refund",),
            prepared_by=str(meta.get("prepared_by") or "workflow"),
        )
        return StepResult(ok=True, data={"refund_id": result.refund_id, "status": result.status})

    if step.step_id == "payments_execute_refund_run":
        result = svc.execute_refund(
            tenant_id,
            refund_id=str(meta.get("refund_id") or ""),
            capabilities=caps,
            approval_id=str(meta.get("approval_id") or ""),
            approved_by=str(meta.get("approved_by") or ""),
            idempotency_key=str(meta.get("idempotency_key") or run_id),
        )
        return StepResult(ok=True, data={"refund_id": result.refund_id, "status": result.status})

    return StepResult(ok=True, data={"step_id": step.step_id})


def register_payments_workflows(definitions, platform) -> None:
    for factory in (
        process_event_definition,
        ingest_statement_definition,
        match_definition,
        allocate_definition,
        reconcile_definition,
        prepare_refund_definition,
        execute_refund_definition,
    ):
        try:
            definitions.register(factory())
        except Exception:
            pass
    for step_id in (
        "payments_process_event_run",
        "payments_ingest_statement_run",
        "payments_match_run",
        "payments_allocate_run",
        "payments_reconcile_run",
        "payments_prepare_refund_run",
        "payments_execute_refund_run",
    ):
        try:
            platform.register_handler(step_id, payments_workflow_handler)
        except Exception:
            pass
