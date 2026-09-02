"""Canonical production business E2E scenario runners."""

from __future__ import annotations

from decimal import Decimal

from business_assistant.models import STATUS_WAITING_FOR_APPROVAL
from integrations.activation.models import ENV_FIXTURE
from integrations.ozon.mapping import build_preview
from production_business_e2e.config import TENANT_A
from production_business_e2e.fixtures import seed_samsung_supplier
from production_business_e2e.models import E2EEvidence, E2EWorld, utc_now_iso
from security.identity import RequestSecurityContext


def _ctx(tenant: str = TENANT_A) -> RequestSecurityContext:
    return RequestSecurityContext(tenant_id=tenant, user_id="e2e-user", roles=("user",), request_id="e2e-req")


def run_supplier_analysis(world: E2EWorld) -> E2EEvidence:
    ev = E2EEvidence(scenario_id="A_supplier_excel_analysis", tenant_id=TENANT_A, status="RUNNING", started_at=utc_now_iso())
    seed_samsung_supplier(world.ba)
    req = world.ba.submit_request(
        tenant_id=TENANT_A,
        user_id="e2e-user",
        text="Возьми прайс Samsung, сравни с прошлым, посчитай маржу и маркетплейсы, покажи перед публикацией",
    )
    plan = world.ba.build_plan(request_id=req.request_id, tenant_id=TENANT_A)
    ex = world.ba.execute(plan_id=plan.plan_id, tenant_id=TENANT_A)
    ev.execution_id = ex.execution_id
    ev.workflow_id = ex.workflow_id
    ev.steps = [{"step": s.name, "capability": s.capability} for s in plan.steps]
    ev.business_result = {"status": ex.status, "findings": len(ex.findings), "artifacts": [a.get("type") for a in ex.artifacts]}
    ev.side_effects = list(world.ba._external_writes)
    if ex.approval:
        ev.approvals.append({"approval_id": ex.approval.approval_id, "status": ex.approval.status})
    return ev.finalize(status="PASS" if ex.status == STATUS_WAITING_FOR_APPROVAL else "FAIL")


def run_marketplace_economics(world: E2EWorld) -> E2EEvidence:
    ev = E2EEvidence(scenario_id="B_marketplace_economics", tenant_id=TENANT_A, status="RUNNING", started_at=utc_now_iso())
    seed_samsung_supplier(world.ba)
    req = world.ba.submit_request(
        tenant_id=TENANT_A,
        user_id="e2e-user",
        text="Проанализируй прайс Samsung маржа маркетплейс",
        read_only=True,
    )
    plan = world.ba.build_plan(request_id=req.request_id, tenant_id=TENANT_A)
    ex = world.ba.execute(plan_id=plan.plan_id, tenant_id=TENANT_A)
    calcs = [f for f in ex.findings if f.kind == "CALCULATION"]
    ev.business_result = {"calculations": len(calcs), "loss_findings": [f.summary for f in ex.findings if "Loss" in f.summary]}
    return ev.finalize(status="PASS" if calcs else "FAIL")


def run_governed_marketplace_write(world: E2EWorld) -> E2EEvidence:
    ev = E2EEvidence(scenario_id="E_governed_marketplace_write", tenant_id=TENANT_A, status="RUNNING", started_at=utc_now_iso())
    store = world.activation._ozon_fixture._store
    preview = build_preview(operation="price_update", before={"seller_price": "1990"}, after={"seller_price": "49990"})
    writes_before = store.write_count("e2e-oz-1")
    try:
        world.activation.execute_via_gateway(
            tenant_id=TENANT_A,
            capability="marketplace.ozon.price.write",
            environment=ENV_FIXTURE,
            operation_class="WRITE",
            payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "49990", "preview": preview},
            idempotency_key="e2e-oz-1",
            approved_write=False,
        )
        ev.errors.append("expected_write_denied")
    except Exception:
        pass
    w1 = world.activation.execute_via_gateway(
        tenant_id=TENANT_A,
        capability="marketplace.ozon.price.write",
        environment=ENV_FIXTURE,
        operation_class="WRITE",
        payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "49990", "preview": preview},
        idempotency_key="e2e-oz-1",
        approved_write=True,
    )
    w2 = world.activation.execute_via_gateway(
        tenant_id=TENANT_A,
        capability="marketplace.ozon.price.write",
        environment=ENV_FIXTURE,
        operation_class="WRITE",
        payload={"operation": "price_update", "seller_article": "OZ-SKU-100", "new_price": "49990", "preview": preview},
        idempotency_key="e2e-oz-1",
        approved_write=True,
    )
    ev.side_effects = [{"write_id": w1["result"].get("write_id"), "idempotent_repeat": w2["result"].get("idempotent")}]
    ev.business_result = {"writes_before": writes_before, "writes_after": store.write_count("e2e-oz-1"), "verified": w1["result"].get("verified")}
    return ev.finalize(status="PASS" if store.write_count("e2e-oz-1") == 1 and w2["result"].get("idempotent") else "FAIL")


def run_analytics_business_question(world: E2EWorld) -> E2EEvidence:
    ev = E2EEvidence(scenario_id="K_analytics_business_question", tenant_id=TENANT_A, status="RUNNING", started_at=utc_now_iso())
    out = world.analytics.ba_query(_ctx(), tenant_id=TENANT_A, question_type="sales_week")
    ev.business_result = out
    return ev.finalize(status="PASS" if out.get("mode") == "FIXTURE" and not out.get("live") else "FAIL")


def run_scheduled_to_analytics(world: E2EWorld) -> E2EEvidence:
    from datetime import datetime, timedelta, timezone

    from scheduled_automation.models import ScheduleDefinition, TARGET_ANALYTICS

    ev = E2EEvidence(scenario_id="L_scheduled_automation", tenant_id=TENANT_A, status="RUNNING", started_at=utc_now_iso())
    now = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)
    world.scheduling._clock.set(now)
    created = world.scheduling.create_schedule(
        _ctx(),
        {
            "tenant_id": TENANT_A,
            "name": "Daily sales",
            "schedule_type": "ONCE",
            "timezone": "UTC",
            "start_at": (now - timedelta(minutes=1)).isoformat(),
            "target_type": TARGET_ANALYTICS,
            "target_payload": {"question_type": "sales_week"},
            "required_capabilities": (),
        },
    )
    ev.schedule_id = created["schedule_id"]
    from scheduled_automation.models import ScheduleDefinition as SD

    s = world.scheduling.store.get_schedule(tenant_id=TENANT_A, schedule_id=created["schedule_id"])
    s = SD(**{**s.__dict__, "next_run_at": now.isoformat()})
    world.scheduling.store.update_schedule(s, expected_version=1)
    tick = world.scheduling.tick(tenant_id=TENANT_A)
    ev.business_result = {"tick": tick, "dispatches": len(world.scheduling._dispatcher.dispatches)}
    return ev.finalize(status="PASS" if tick and tick[0].get("status") == "DISPATCHED" else "FAIL")
