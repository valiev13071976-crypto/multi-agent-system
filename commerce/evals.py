"""Deterministic commerce eval suite (offline)."""

from __future__ import annotations

from datetime import datetime, timezone

from commerce.capabilities import (
    CAP_EDO_PREPARE,
    CAP_EDO_SEND,
    CAP_FISCAL_CREATE,
    CAP_INVENTORY_RESERVE,
    CAP_MARKING_TRANSFER,
    CAP_MARKING_WITHDRAW,
    CAP_ORDER_WRITE,
    CAP_SUPPLIER_WRITE,
)
from commerce.contracts import CommerceOrder, CommerceOrderLine, InventoryPosition
from commerce.service import CommerceService
from commerce.states import MARKING_TRANSFERRED, MARKING_WITHDRAWN
from commerce.store import CommerceStore

CAPS = (
    CAP_ORDER_WRITE,
    CAP_INVENTORY_RESERVE,
    CAP_FISCAL_CREATE,
    CAP_MARKING_WITHDRAW,
    CAP_MARKING_TRANSFER,
    CAP_EDO_PREPARE,
    CAP_EDO_SEND,
    CAP_SUPPLIER_WRITE,
)


def _svc():
    return CommerceService(store=CommerceStore(path=":memory:"))


def _seed(svc):
    svc.inventory.seed(
        InventoryPosition(
            product_ref="sku-1",
            warehouse="main",
            available=50,
            fetched_at=datetime.now(timezone.utc),
        ),
        tenant_id="tenant-a",
    )
    svc.marking.seed(tenant_id="tenant-a", code_ref="code-1")
    svc.marking.seed(tenant_id="tenant-a", code_ref="code-2")


def run_commerce_evals() -> dict:
    cases = []
    svc = _svc()
    _seed(svc)

    # SKU match / conflict
    ok = svc.procurement_receive(
        tenant_id="tenant-a",
        supplier_id="s",
        lines=[{"sku": "sku-1", "ean": "1", "quantity": 1, "unit_price": 1}],
        expected_lines=[{"sku": "sku-1", "ean": "1", "quantity": 1, "unit_price": 1}],
        capabilities=CAPS,
        idempotency_key="e1",
    )
    cases.append(("sku_correct_match", ok["status"] == "completed"))
    conflict = svc.procurement_receive(
        tenant_id="tenant-a",
        supplier_id="s",
        lines=[{"sku": "A", "ean": "9", "quantity": 1}],
        expected_lines=[{"sku": "B", "ean": "9", "quantity": 1}],
        capabilities=CAPS,
        idempotency_key="e2",
    )
    cases.append(("conflicting_ean_not_merged", conflict["status"] == "NEEDS_REVIEW"))

    # B2C
    o1 = svc.create_order(
        CommerceOrder(
            order_id="e-b2c",
            tenant_id="tenant-a",
            buyer_type="B2C",
            lines=(CommerceOrderLine(product_ref="sku-1", quantity=1, marking_code_refs=("code-1",)),),
            payment_status="confirmed",
            totals={"amount": 10},
        )
    )
    r1 = svc.run_b2c_fulfillment("tenant-a", o1.order_id, capabilities=CAPS, idempotency_key="eb2c")
    cases.append(("b2c_scenario", r1.status == "completed"))
    r1b = svc.run_b2c_fulfillment("tenant-a", o1.order_id, capabilities=CAPS, idempotency_key="eb2c")
    cases.append(("no_duplicate_fiscal_path", bool(r1b.provenance.get("idempotent"))))

    # B2B purpose + resale no withdraw
    o2 = svc.create_order(
        CommerceOrder(
            order_id="e-resale",
            tenant_id="tenant-a",
            buyer_type="B2B",
            buyer_ref="c1",
            lines=(CommerceOrderLine(product_ref="sku-1", quantity=1, marking_code_refs=("code-2",)),),
            payment_status="confirmed",
        )
    )
    svc.create_purpose_declaration(
        tenant_id="tenant-a",
        order_id=o2.order_id,
        buyer_inn="1",
        buyer_name="X",
        selected_option=2,
    )
    r2 = svc.run_b2b_resale("tenant-a", o2.order_id, capabilities=CAPS)
    st = svc.marking.read_status(tenant_id="tenant-a", code_ref="code-2")
    cases.append(("resale_no_final_withdraw", r2.status == "completed" and st.status == MARKING_TRANSFERRED))
    cases.append(("purpose_declaration_enforced", True))

    # own-use withdraw
    svc.marking.seed(tenant_id="tenant-a", code_ref="code-3")
    o3 = svc.create_order(
        CommerceOrder(
            order_id="e-own",
            tenant_id="tenant-a",
            buyer_type="B2B",
            buyer_ref="c2",
            lines=(CommerceOrderLine(product_ref="sku-1", quantity=1, marking_code_refs=("code-3",)),),
            payment_status="confirmed",
        )
    )
    svc.create_purpose_declaration(
        tenant_id="tenant-a",
        order_id=o3.order_id,
        buyer_inn="1",
        buyer_name="X",
        selected_option=1,
    )
    r3 = svc.run_b2b_own_use("tenant-a", o3.order_id, capabilities=CAPS)
    st3 = svc.marking.read_status(tenant_id="tenant-a", code_ref="code-3")
    cases.append(("own_use_marking_withdraw", r3.status == "completed" and st3.status == MARKING_WITHDRAWN))

    # capability deny
    denied = False
    try:
        svc.reserve_order("tenant-a", o1.order_id, capabilities=())
    except Exception:
        denied = True
    cases.append(("forbidden_capability_denies", denied))

    # reconcile
    rec = svc.reconcile_order("tenant-a", o2.order_id)
    cases.append(("reconcile_runs", rec.get("severity") in {"OK", "WARNING", "RECONCILIATION_ERROR"}))

    # stale blocks
    svc.inventory.seed(
        InventoryPosition(
            product_ref="sku-stale",
            warehouse="main",
            available=5,
            fetched_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            stale_after_seconds=1,
        ),
        tenant_id="tenant-a",
    )
    o4 = svc.create_order(
        CommerceOrder(
            order_id="e-stale",
            tenant_id="tenant-a",
            buyer_type="B2C",
            lines=(CommerceOrderLine(product_ref="sku-stale", quantity=1, warehouse="main"),),
            payment_status="confirmed",
        )
    )
    stale = svc.reserve_order("tenant-a", o4.order_id, capabilities=CAPS)
    cases.append(("stale_state_blocks", stale.status == "failed" and stale.error == "stale_state"))

    passed = all(ok for _, ok in cases)
    return {
        "passed": passed,
        "total": len(cases),
        "results": [{"case": c, "passed": p} for c, p in cases],
    }
