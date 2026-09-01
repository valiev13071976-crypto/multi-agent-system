"""Serialize/hydrate Business Assistant state for durable API recovery."""

from __future__ import annotations

from decimal import Decimal

from business_assistant.models import (
    BusinessApprovalRequest,
    BusinessConstraint,
    BusinessExecution,
    BusinessExecutionStep,
    BusinessFinding,
    BusinessPlan,
    BusinessPlanStep,
    BusinessPreview,
    BusinessRequest,
)


def _ser_decimal(v) -> str | None:
    return str(v) if v is not None else None


def snapshot_ba_service(ba) -> dict:
    """Capture BA in-memory state needed for resume/approval after reload."""
    return {
        "fixture_rows": list(ba._fixture_rows),
        "previous_prices": {k: str(v) for k, v in ba._previous_prices.items()},
        "market_obs": {k: str(v) for k, v in ba._market_obs.items()},
        "costs": {k: str(v) for k, v in ba._costs.items()},
        "fixture_catalog": list(ba._fixture_catalog),
        "external_writes": list(ba._external_writes),
        "requests": {k: _ser_request(v) for k, v in ba._requests.items()},
        "plans": {k: _ser_plan(v) for k, v in ba._plans.items()},
        "executions": {k: _ser_execution(v) for k, v in ba._executions.items()},
    }


def hydrate_ba_service(ba, snap: dict) -> None:
    ba._fixture_rows = list(snap.get("fixture_rows") or [])
    ba._previous_prices = {k: Decimal(v) for k, v in (snap.get("previous_prices") or {}).items()}
    ba._market_obs = {k: Decimal(v) for k, v in (snap.get("market_obs") or {}).items()}
    ba._costs = {k: Decimal(v) for k, v in (snap.get("costs") or {}).items()}
    ba._fixture_catalog = list(snap.get("fixture_catalog") or [])
    ba._external_writes = list(snap.get("external_writes") or [])
    ba._requests = {k: _des_request(v) for k, v in (snap.get("requests") or {}).items()}
    ba._plans = {k: _des_plan(v) for k, v in (snap.get("plans") or {}).items()}
    ba._executions = {k: _des_execution(v) for k, v in (snap.get("executions") or {}).items()}


def _ser_request(r: BusinessRequest) -> dict:
    c = r.constraints
    return {
        "request_id": r.request_id,
        "tenant_id": r.tenant_id,
        "user_id": r.user_id,
        "text": r.text,
        "intent": r.intent,
        "objective": r.objective,
        "constraints": {
            "brands": list(c.brands),
            "read_only": c.read_only,
            "show_before_publication": c.show_before_publication,
            "margin_min_pct": _ser_decimal(c.margin_min_pct),
            "top_n": c.top_n,
        },
        "artifact_refs": list(r.artifact_refs),
        "correlation_id": r.correlation_id,
        "read_only": r.read_only,
    }


def _des_request(d: dict) -> BusinessRequest:
    c = d.get("constraints") or {}
    margin = c.get("margin_min_pct")
    return BusinessRequest(
        request_id=d["request_id"],
        tenant_id=d["tenant_id"],
        user_id=d["user_id"],
        text=d["text"],
        intent=d["intent"],
        objective=d["objective"],
        constraints=BusinessConstraint(
            brands=tuple(c.get("brands") or ()),
            read_only=bool(c.get("read_only")),
            show_before_publication=bool(c.get("show_before_publication")),
            margin_min_pct=Decimal(margin) if margin is not None else None,
            top_n=c.get("top_n"),
        ),
        artifact_refs=tuple(d.get("artifact_refs") or ()),
        correlation_id=d.get("correlation_id") or "",
        read_only=bool(d.get("read_only")),
    )


def _ser_plan(p: BusinessPlan) -> dict:
    return {
        "plan_id": p.plan_id,
        "tenant_id": p.tenant_id,
        "request_id": p.request_id,
        "version": p.version,
        "recipe": p.recipe,
        "fingerprint": p.fingerprint,
        "read_only": p.read_only,
        "approval_boundaries": list(p.approval_boundaries),
        "steps": [
            {
                "step_id": s.step_id,
                "name": s.name,
                "capability": s.capability,
                "step_class": s.step_class,
                "depends_on": list(s.depends_on),
                "requires_approval": s.requires_approval,
                "workload": s.workload,
            }
            for s in p.steps
        ],
    }


def _des_plan(d: dict) -> BusinessPlan:
    steps = tuple(
        BusinessPlanStep(
            step_id=s["step_id"],
            name=s["name"],
            capability=s["capability"],
            step_class=s["step_class"],
            depends_on=tuple(s.get("depends_on") or ()),
            requires_approval=bool(s.get("requires_approval")),
            workload=s.get("workload") or "interactive",
        )
        for s in d.get("steps") or []
    )
    return BusinessPlan(
        plan_id=d["plan_id"],
        tenant_id=d["tenant_id"],
        request_id=d["request_id"],
        version=int(d.get("version") or 1),
        recipe=d.get("recipe") or "",
        steps=steps,
        fingerprint=d["fingerprint"],
        read_only=bool(d.get("read_only")),
        approval_boundaries=tuple(d.get("approval_boundaries") or ()),
    )


def _ser_execution(ex: BusinessExecution) -> dict:
    return {
        "execution_id": ex.execution_id,
        "tenant_id": ex.tenant_id,
        "request_id": ex.request_id,
        "plan_id": ex.plan_id,
        "plan_fingerprint": ex.plan_fingerprint,
        "status": ex.status,
        "steps": {
            k: {
                "step_id": v.step_id,
                "status": v.status,
                "result": dict(v.result or {}),
                "error_code": v.error_code,
            }
            for k, v in ex.steps.items()
        },
        "findings": [
            {
                "finding_id": f.finding_id,
                "kind": f.kind,
                "summary": f.summary,
                "evidence_refs": list(f.evidence_refs),
                "confidence": str(f.confidence),
                "sku_id": f.sku_id,
                "numeric_value": f.numeric_value,
            }
            for f in ex.findings
        ],
        "artifacts": list(ex.artifacts),
        "preview": _ser_preview(ex.preview) if ex.preview else None,
        "approval": _ser_approval(ex.approval) if ex.approval else None,
        "cost": str(ex.cost),
        "correlation_id": ex.correlation_id,
        "workflow_id": ex.workflow_id,
        "checkpoint": ex.checkpoint,
        "cancelled": ex.cancelled,
        "summary": ex.summary,
        "mode": ex.mode,
    }


def _des_execution(d: dict) -> BusinessExecution:
    steps = {
        k: BusinessExecutionStep(
            step_id=v["step_id"],
            status=v["status"],
            result=dict(v.get("result") or {}),
            error_code=v.get("error_code") or "",
        )
        for k, v in (d.get("steps") or {}).items()
    }
    findings = [
        BusinessFinding(
            finding_id=f["finding_id"],
            kind=f["kind"],
            summary=f["summary"],
            evidence_refs=tuple(f.get("evidence_refs") or ()),
            confidence=Decimal(f.get("confidence") or "1.0"),
            sku_id=f.get("sku_id") or "",
            numeric_value=f.get("numeric_value") or "",
        )
        for f in d.get("findings") or []
    ]
    preview = _des_preview(d["preview"]) if d.get("preview") else None
    approval = _des_approval(d["approval"]) if d.get("approval") else None
    return BusinessExecution(
        execution_id=d["execution_id"],
        tenant_id=d["tenant_id"],
        request_id=d["request_id"],
        plan_id=d["plan_id"],
        plan_fingerprint=d["plan_fingerprint"],
        status=d["status"],
        steps=steps,
        findings=findings,
        artifacts=list(d.get("artifacts") or []),
        preview=preview,
        approval=approval,
        cost=Decimal(d.get("cost") or "0"),
        correlation_id=d.get("correlation_id") or "",
        workflow_id=d.get("workflow_id") or "",
        checkpoint=int(d.get("checkpoint") or 0),
        cancelled=bool(d.get("cancelled")),
        summary=d.get("summary") or "",
        mode=d.get("mode") or "FIXTURE",
    )


def _ser_preview(p: BusinessPreview) -> dict:
    return {
        "preview_id": p.preview_id,
        "tenant_id": p.tenant_id,
        "execution_id": p.execution_id,
        "plan_fingerprint": p.plan_fingerprint,
        "artifact_checksum": p.artifact_checksum,
        "changes": list(p.changes),
        "warnings": list(p.warnings),
        "external_writes": list(p.external_writes),
    }


def _des_preview(d: dict) -> BusinessPreview:
    return BusinessPreview(
        preview_id=d["preview_id"],
        tenant_id=d["tenant_id"],
        execution_id=d["execution_id"],
        plan_fingerprint=d["plan_fingerprint"],
        artifact_checksum=d["artifact_checksum"],
        changes=tuple(d.get("changes") or ()),
        warnings=tuple(d.get("warnings") or ()),
        external_writes=tuple(d.get("external_writes") or ()),
    )


def _ser_approval(a: BusinessApprovalRequest) -> dict:
    return {
        "approval_id": a.approval_id,
        "tenant_id": a.tenant_id,
        "execution_id": a.execution_id,
        "plan_fingerprint": a.plan_fingerprint,
        "preview_id": a.preview_id,
        "actor_id": a.actor_id,
        "step_ids": list(a.step_ids),
        "status": a.status,
    }


def _des_approval(d: dict) -> BusinessApprovalRequest:
    return BusinessApprovalRequest(
        approval_id=d["approval_id"],
        tenant_id=d["tenant_id"],
        execution_id=d["execution_id"],
        plan_fingerprint=d["plan_fingerprint"],
        preview_id=d["preview_id"],
        actor_id=d.get("actor_id") or "",
        step_ids=tuple(d.get("step_ids") or ()),
        status=d.get("status") or "PENDING",
    )
