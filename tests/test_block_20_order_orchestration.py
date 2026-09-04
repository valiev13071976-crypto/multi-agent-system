"""Block 20 — order orchestration targeted tests. Offline fixture only."""

from __future__ import annotations

import unittest
from decimal import Decimal

from commerce_ops.service import CommerceOpsService
from data_intel.economics import EconomicsInput, PROV_CONFIGURED
from governed_publish.contracts import MODE_LIVE, STATUS_BLOCKED, STATUS_EXECUTED_FIXTURE
from order_orchestration.contracts import (
    AGG_PARTIAL,
    AMBIGUOUS,
    INGEST_REPLAY,
    MAPPED,
    MISSING,
    SOURCE_OZON,
    SOURCE_SITE,
    SOURCE_WB,
    SOURCE_YM,
    STATUS_CANCELLED,
    STATUS_REQUIRES_REVIEW,
)
from order_orchestration.errors import (
    AMBIGUOUS_PRODUCT_MAPPING,
    APPROVAL_REJECTED,
    CAPABILITY_DENIED,
    CURRENCY_MISMATCH,
    INVALID_PRICE,
    INVALID_QUANTITY,
    LIVE_FORBIDDEN,
    MISSING_ORDER_ID,
    MISSING_PRODUCT_MAPPING,
    ONEC_BLOCKED,
    ORDER_ACCESS_DENIED,
    ORDER_CONFLICT,
    STALE_APPROVAL,
    STALE_ORDER_EVENT,
    UNSUPPORTED_SOURCE,
    OrderOrchError,
)
from order_orchestration.service import CRM_WRITE_CAP, ONEC_WRITE_CAP, OrderOrchestrationService
from product_content.service import ProductContentService


def _order(*, source=SOURCE_SITE, ext="EXT-1", sku="SKU-1", qty="1", price="100", **extra):
    line = {"sku": sku, "quantity": qty, "unit_price": price, "product_id": extra.pop("product_id", "prod-1")}
    if extra.pop("omit_price", False):
        line.pop("unit_price")
    raw = {
        "source": source,
        "external_order_id": ext,
        "source_status": extra.pop("source_status", "NEW"),
        "source_order_version": extra.pop("version", "1"),
        "currency": extra.pop("currency", "RUB"),
        "customer_ref": extra.pop("customer_ref", "cust-fixture-1"),
        "lines": extra.pop("lines", [line]),
    }
    raw.update(extra)
    return raw


class Block20Tests(unittest.TestCase):
    def setUp(self):
        self.svc = OrderOrchestrationService()
        self.cat = {"SKU-1": "prod-1", "SKU-WB": "prod-1", "SKU-OZ": "prod-1", "SKU-YM": "prod-1"}

    def test_ingest_sources_validation_idempotency(self):
        a = self.svc.ingest(_order(), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(a.source, SOURCE_SITE)
        self.assertEqual(a.lines[0].mapping_status, MAPPED)
        self.assertEqual(a.payment_state, "UNKNOWN")
        self.assertEqual(a.fulfillment_state, "UNKNOWN")
        self.assertEqual(a.source_status, "NEW")
        self.assertIsNone(a.order_total)
        self.assertIsNone(a.contribution_estimate)
        replay = self.svc.ingest(_order(), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(replay.order_id, a.order_id)
        self.assertEqual(replay.ingest_result, INGEST_REPLAY)
        wb = self.svc.ingest(_order(source=SOURCE_WB, ext="WB-1", sku="SKU-WB"), tenant_id="t-a", catalog=self.cat)
        oz = self.svc.ingest(_order(source=SOURCE_OZON, ext="OZ-1", sku="SKU-OZ"), tenant_id="t-a", catalog=self.cat)
        ym = self.svc.ingest(_order(source=SOURCE_YM, ext="YM-1", sku="SKU-YM"), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(wb.source, SOURCE_WB)
        self.assertEqual(oz.source, SOURCE_OZON)
        self.assertEqual(ym.source, SOURCE_YM)
        multi = self.svc.ingest(
            _order(
                ext="M-1",
                lines=[
                    {"sku": "SKU-1", "quantity": "1", "unit_price": "10", "product_id": "prod-1"},
                    {"sku": "SKU-1", "quantity": "2", "unit_price": "10", "product_id": "prod-1"},
                ],
            ),
            tenant_id="t-a",
            catalog=self.cat,
        )
        self.assertEqual(len(multi.lines), 2)
        miss = self.svc.ingest(_order(ext="MISS", sku="NOPE", product_id=""), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(miss.lines[0].mapping_status, MISSING)
        self.assertEqual(miss.canonical_status, STATUS_REQUIRES_REVIEW)
        amb = self.svc.ingest(
            _order(ext="AMB", sku="AMB1", product_id=""),
            tenant_id="t-a",
            catalog={"AMB1": ["p-a", "p-b"]},
        )
        self.assertEqual(amb.lines[0].mapping_status, AMBIGUOUS)
        with self.assertRaises(OrderOrchError) as q:
            self.svc.ingest(_order(ext="Q", qty="0"), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(q.exception.code, INVALID_QUANTITY)
        with self.assertRaises(OrderOrchError) as p:
            self.svc.ingest(_order(ext="P", price="-1"), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(p.exception.code, INVALID_PRICE)
        unk_price = self.svc.ingest(_order(ext="U", omit_price=True), tenant_id="t-a", catalog=self.cat)
        self.assertIsNone(unk_price.lines[0].unit_price)
        with self.assertRaises(OrderOrchError) as ccy:
            self.svc.ingest(
                _order(ext="C", lines=[{"sku": "SKU-1", "quantity": "1", "unit_price": "1", "currency": "USD", "product_id": "prod-1"}]),
                tenant_id="t-a",
                catalog=self.cat,
            )
        self.assertEqual(ccy.exception.code, CURRENCY_MISMATCH)
        with self.assertRaises(OrderOrchError):
            self.svc.ingest({"source": SOURCE_SITE, "lines": [{"sku": "SKU-1", "quantity": "1"}]}, tenant_id="t-a")
        with self.assertRaises(OrderOrchError) as src:
            self.svc.ingest(_order(source="AMAZON", ext="X"), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(src.exception.code, UNSUPPORTED_SOURCE)
        with self.assertRaises(OrderOrchError) as conf:
            self.svc.ingest(
                _order(
                    ext="EXT-1",
                    version="2",
                    lines=[{"sku": "SKU-1", "quantity": "9", "unit_price": "1", "product_id": "prod-1"}],
                ),
                tenant_id="t-a",
                catalog=self.cat,
            )
        self.assertEqual(conf.exception.code, ORDER_CONFLICT)
        v2 = self.svc.ingest(_order(ext="V", version="2"), tenant_id="t-a", catalog=self.cat)
        with self.assertRaises(OrderOrchError) as stale:
            self.svc.ingest(_order(ext="V", version="1", source_status="CANCELLED"), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(stale.exception.code, STALE_ORDER_EVENT)
        can = self.svc.ingest(_order(ext="CAN", version="1"), tenant_id="t-a", catalog=self.cat)
        can2 = self.svc.ingest(_order(ext="CAN", version="2", source_status="CANCELLED"), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(can2.canonical_status, STATUS_CANCELLED)
        self.assertEqual(can2.order_id, can.order_id)
        can3 = self.svc.ingest(_order(ext="CAN", version="2", source_status="CANCELLED"), tenant_id="t-a", catalog=self.cat)
        self.assertEqual(can3.ingest_result, INGEST_REPLAY)
        other = self.svc.ingest(_order(ext="TB"), tenant_id="t-b", catalog=self.cat)
        with self.assertRaises(OrderOrchError) as iso:
            self.svc.get_order(other.order_id, tenant_id="t-a")
        self.assertEqual(iso.exception.code, ORDER_ACCESS_DENIED)
        events = self.svc.store.list_audit(tenant_id="t-a")
        blob = str(events)
        self.assertNotIn("phone", blob.casefold())
        self.assertNotIn("@", blob)

    def test_onec_crm_hitl_partial_recovery(self):
        order = self.svc.ingest(_order(ext="DS-1"), tenant_id="t-a", catalog=self.cat)
        onec_caps = {ONEC_WRITE_CAP}
        crm_caps = {CRM_WRITE_CAP}
        plan = self.svc.plan_onec(order, tenant_id="t-a", requested_by="agent")
        self.assertEqual(plan.status, "APPROVAL_REQUIRED")
        with self.assertRaises(OrderOrchError) as cap:
            self.svc.approve(plan.plan_id, tenant_id="t-a", actor="reviewer")
            self.svc.execute_downstream(plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=set(), order=order)
        self.assertEqual(cap.exception.code, CAPABILITY_DENIED)
        rec = self.svc.execute_downstream(plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=onec_caps, order=order)
        self.assertEqual(rec.status, STATUS_EXECUTED_FIXTURE)
        self.assertTrue(rec.fixture_reference.startswith("fixture:onec:"))
        self.assertFalse(rec.published_live)
        replay = self.svc.execute_downstream(plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=onec_caps, order=order)
        self.assertEqual(replay.status, "ALREADY_EXECUTED")

        miss = self.svc.ingest(_order(ext="DS-MISS", sku="NO", product_id=""), tenant_id="t-a", catalog=self.cat)
        blocked = self.svc.plan_onec(miss, tenant_id="t-a", requested_by="agent")
        self.assertEqual(blocked.status, STATUS_BLOCKED)
        with self.assertRaises(OrderOrchError) as b:
            self.svc.execute_downstream(blocked.plan_id, tenant_id="t-a", actor="r", capabilities=onec_caps, order=miss)
        self.assertEqual(b.exception.code, ONEC_BLOCKED)

        rej_o = self.svc.ingest(_order(ext="DS-REJ"), tenant_id="t-a", catalog=self.cat)
        rp = self.svc.plan_onec(rej_o, tenant_id="t-a", requested_by="agent")
        self.svc.reject(rp.plan_id, tenant_id="t-a", actor="reviewer")
        with self.assertRaises(OrderOrchError) as rj:
            self.svc.execute_downstream(rp.plan_id, tenant_id="t-a", actor="reviewer", capabilities=onec_caps, order=rej_o)
        self.assertEqual(rj.exception.code, APPROVAL_REJECTED)

        st_o = self.svc.ingest(_order(ext="DS-ST"), tenant_id="t-a", catalog=self.cat)
        sp = self.svc.plan_onec(st_o, tenant_id="t-a", requested_by="agent")
        self.svc.approve(sp.plan_id, tenant_id="t-a", actor="reviewer")
        changed = self.svc.ingest(_order(ext="DS-ST", version="2", source_status="CONFIRMED"), tenant_id="t-a", catalog=self.cat)
        with self.assertRaises(OrderOrchError) as st:
            self.svc.execute_downstream(sp.plan_id, tenant_id="t-a", actor="reviewer", capabilities=onec_caps, order=changed)
        self.assertEqual(st.exception.code, STALE_APPROVAL)

        can = self.svc.ingest(_order(ext="DS-CAN", version="3", source_status="CANCELLED"), tenant_id="t-a", catalog=self.cat)
        cp = self.svc.plan_onec(can, tenant_id="t-a", requested_by="agent", action="CANCEL_ORDER")
        self.svc.approve(cp.plan_id, tenant_id="t-a", actor="reviewer")
        crec = self.svc.execute_downstream(cp.plan_id, tenant_id="t-a", actor="reviewer", capabilities=onec_caps, order=can)
        self.assertEqual(crec.status, STATUS_EXECUTED_FIXTURE)

        with self.assertRaises(OrderOrchError) as live:
            self.svc.ingest(_order(ext="LIVE"), tenant_id="t-a", catalog=self.cat, mode=MODE_LIVE)
        self.assertEqual(live.exception.code, LIVE_FORBIDDEN)

        deal = self.svc.plan_crm(order, tenant_id="t-a", requested_by="agent", action="CREATE_OR_UPDATE_DEAL")
        self.assertEqual(deal.status, STATUS_BLOCKED)
        self.assertIn("DOWNSTREAM_UNSUPPORTED", deal.issues)

        crm_plan = self.svc.plan_crm(order, tenant_id="t-a", requested_by="agent")
        self.svc.approve(crm_plan.plan_id, tenant_id="t-a", actor="reviewer")
        crm_rec = self.svc.execute_downstream(crm_plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=crm_caps, order=order)
        self.assertEqual(crm_rec.status, STATUS_EXECUTED_FIXTURE)
        self.assertTrue(crm_rec.fixture_reference.startswith("fixture:crm:"))
        crm_replay = self.svc.execute_downstream(crm_plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=crm_caps, order=order)
        self.assertEqual(crm_replay.status, "ALREADY_EXECUTED")

        agg = self.svc.aggregate(onec_status=STATUS_EXECUTED_FIXTURE, crm_status=STATUS_BLOCKED)
        self.assertEqual(agg, AGG_PARTIAL)

        tb = self.svc.ingest(_order(ext="ISO"), tenant_id="t-b", catalog=self.cat)
        pb = self.svc.plan_onec(tb, tenant_id="t-b", requested_by="agent")
        with self.assertRaises(OrderOrchError):
            self.svc.plans.get_plan(pb.plan_id, tenant_id="t-a")

    def test_cross_block_and_boundaries(self):
        econ = EconomicsInput(
            sku="SKU-1",
            purchase_price=Decimal("50"),
            purchase_price_prov=PROV_CONFIGURED,
            selling_price=Decimal("100"),
            selling_price_prov=PROV_CONFIGURED,
            commission_rate=Decimal("0"),
            commission_prov=PROV_CONFIGURED,
            advertising_cost=Decimal("0"),
            advertising_prov=PROV_CONFIGURED,
            currency="RUB",
        )
        order = self.svc.ingest(_order(ext="XB-1", sale_price="100"), tenant_id="t-a", catalog=self.cat, economics=econ)
        self.assertEqual(order.economics_reference.get("engine"), "data_intel.economics")
        self.assertIn("Not net profit", order.economics_reference.get("note") or "")
        plan = self.svc.plan_onec(order, tenant_id="t-a", requested_by="agent")
        self.svc.approve(plan.plan_id, tenant_id="t-a", actor="reviewer")
        rec = self.svc.execute_downstream(plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities={ONEC_WRITE_CAP}, order=order)
        crm = self.svc.plan_crm(order, tenant_id="t-a", requested_by="agent")
        self.svc.approve(crm.plan_id, tenant_id="t-a", actor="reviewer")
        crec = self.svc.execute_downstream(crm.plan_id, tenant_id="t-a", actor="reviewer", capabilities={CRM_WRITE_CAP}, order=order)
        self.assertEqual(rec.status, STATUS_EXECUTED_FIXTURE)
        self.assertEqual(crec.status, STATUS_EXECUTED_FIXTURE)
        back = self.svc.get_order(order.order_id, tenant_id="t-a")
        self.assertEqual(back.external_order_id, "XB-1")
        self.assertTrue(CommerceOpsService)
        self.assertNotIn("price_update", rec.action)
        self.assertNotIn("stock", rec.action.casefold())
        events = [e["event"] for e in self.svc.store.list_audit(tenant_id="t-a")]
        self.assertIn("ONEC_EXECUTED_FIXTURE", events)
        self.assertIn("CRM_EXECUTED_FIXTURE", events)


if __name__ == "__main__":
    unittest.main()
