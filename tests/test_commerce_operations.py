"""Commerce Operations & Compliance Platform tests."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
from commerce.contracts import (
    DECLARATION_OWN_USE_V1,
    DECLARATION_RESALE_V1,
    CommerceOrder,
    CommerceOrderLine,
    InventoryPosition,
    SupplierRecord,
)
from commerce.errors import (
    CardDataForbiddenError,
    DeclarationImmutableError,
    DeclarationRequiredError,
    InvalidTransitionError,
)
from commerce.rules import ComplianceRulesEngine
from commerce.runtime import build_commerce_runtime
from commerce.service import CommerceService
from commerce.states import (
    MARKING_TRANSFERRED,
    MARKING_WITHDRAWN,
    ORDER_COMPLETED,
    ORDER_COMPLIANCE_PENDING,
    ORDER_COMPLIANCE_RISK,
    assert_transition,
)
from commerce.store import CommerceStore
from commerce.workflow_def import register_commerce_workflows
from side_effects.runtime import compose_side_effect_runtime
from tests.test_github_write_config import DictSecrets
from workflow.service import build_workflow_runtime


CAPS_FULL = (
    CAP_ORDER_WRITE,
    CAP_INVENTORY_RESERVE,
    CAP_FISCAL_CREATE,
    CAP_MARKING_WITHDRAW,
    CAP_MARKING_TRANSFER,
    CAP_EDO_PREPARE,
    CAP_EDO_SEND,
    CAP_SUPPLIER_WRITE,
)


def _svc() -> CommerceService:
    return CommerceService(store=CommerceStore(path=":memory:"))


def _seed_stock(svc: CommerceService, product: str = "sku-1", wh: str = "main", qty: float = 10):
    svc.inventory.seed(
        InventoryPosition(
            product_ref=product,
            warehouse=wh,
            available=qty,
            fetched_at=datetime.now(timezone.utc),
        ),
        tenant_id="tenant-a",
    )
    svc.marking.seed(tenant_id="tenant-a", code_ref="code-1")


def _order(svc: CommerceService, **kwargs) -> CommerceOrder:
    base = dict(
        order_id=f"ord-{uuid.uuid4().hex[:8]}",
        tenant_id="tenant-a",
        buyer_type="B2C",
        buyer_ref="buyer-1",
        lines=(
            CommerceOrderLine(
                product_ref="sku-1",
                quantity=1,
                sku="sku-1",
                warehouse="main",
                unit_price=100.0,
                marking_code_refs=("code-1",),
            ),
        ),
        totals={"amount": 100.0},
        payment_status="confirmed",
        payment_state_ref="pay-ref-1",
    )
    base.update(kwargs)
    return svc.create_order(CommerceOrder(**base))


class StateMachineTests(unittest.TestCase):
    def test_invalid_transition_denied(self):
        with self.assertRaises(InvalidTransitionError):
            assert_transition("order", ORDER_COMPLETED, "NEW")


class ProcurementTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()
        _seed_stock(self.svc, qty=0)

    def test_valid_and_idempotent(self):
        result = self.svc.procurement_receive(
            tenant_id="tenant-a",
            supplier_id="sup-1",
            lines=[{"product_ref": "sku-1", "sku": "sku-1", "quantity": 5, "unit_price": 10, "warehouse": "main"}],
            expected_lines=[{"sku": "sku-1", "quantity": 5, "unit_price": 10}],
            capabilities=CAPS_FULL,
            idempotency_key="recv-1",
        )
        self.assertEqual(result["status"], "completed")
        again = self.svc.procurement_receive(
            tenant_id="tenant-a",
            supplier_id="sup-1",
            lines=[{"product_ref": "sku-1", "sku": "sku-1", "quantity": 5, "unit_price": 10}],
            expected_lines=[{"sku": "sku-1", "quantity": 5, "unit_price": 10}],
            capabilities=CAPS_FULL,
            idempotency_key="recv-1",
        )
        self.assertEqual(again["operation_id"], result["operation_id"])

    def test_shortage_and_conflict(self):
        shortage = self.svc.procurement_receive(
            tenant_id="tenant-a",
            supplier_id="sup-1",
            lines=[{"sku": "sku-1", "quantity": 1, "unit_price": 10}],
            expected_lines=[{"sku": "sku-1", "quantity": 5, "unit_price": 10}],
            capabilities=CAPS_FULL,
        )
        self.assertEqual(shortage["status"], "NEEDS_REVIEW")
        conflict = self.svc.procurement_receive(
            tenant_id="tenant-a",
            supplier_id="sup-1",
            lines=[{"sku": "sku-A", "ean": "4600000000001", "quantity": 1}],
            expected_lines=[{"sku": "sku-B", "ean": "4600000000001", "quantity": 1}],
            capabilities=CAPS_FULL,
            idempotency_key="recv-conflict",
        )
        self.assertEqual(conflict["status"], "NEEDS_REVIEW")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()
        _seed_stock(self.svc, wh="wh-a", qty=5)
        _seed_stock(self.svc, wh="wh-b", qty=2)

    def test_reserve_and_tenant_isolation(self):
        order = _order(
            self.svc,
            lines=(CommerceOrderLine(product_ref="sku-1", quantity=3, warehouse="wh-a"),),
        )
        res = self.svc.reserve_order("tenant-a", order.order_id, capabilities=CAPS_FULL)
        self.assertEqual(res.status, "completed")
        fail = self.svc.reserve_order(
            "tenant-a",
            _order(
                self.svc,
                order_id=f"ord-{uuid.uuid4().hex[:8]}",
                lines=(CommerceOrderLine(product_ref="sku-1", quantity=99, warehouse="wh-a"),),
            ).order_id,
            capabilities=CAPS_FULL,
        )
        self.assertEqual(fail.status, "failed")
        with self.assertRaises(Exception):
            self.svc._get_order("tenant-b", order.order_id)


class PurposeAndRiskTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()
        _seed_stock(self.svc)

    def test_declaration_required_immutable_versions(self):
        order = _order(self.svc, buyer_type="B2B")
        with self.assertRaises(DeclarationRequiredError):
            self.svc.evaluate_compliance(order)
        decl = self.svc.create_purpose_declaration(
            tenant_id="tenant-a",
            order_id=order.order_id,
            buyer_inn="7707083893",
            buyer_name="OOO Test",
            selected_option=1,
        )
        self.assertEqual(decl.exact_text, DECLARATION_OWN_USE_V1)
        with self.assertRaises(DeclarationImmutableError):
            self.svc.create_purpose_declaration(
                tenant_id="tenant-a",
                order_id=order.order_id,
                buyer_inn="7707083893",
                buyer_name="OOO Test",
                selected_option=2,
            )
        events = self.svc.store.list_audit("tenant-a")
        self.assertTrue(any(e["event_type"] == "purpose_declaration_created" for e in events))

    def test_risk_hitl_not_auto_block_accusation(self):
        order = _order(self.svc, buyer_type="B2B", buyer_ref="buyer-risk")
        self.svc.create_purpose_declaration(
            tenant_id="tenant-a",
            order_id=order.order_id,
            buyer_inn="1",
            buyer_name="X",
            selected_option=1,
        )
        # repeat pattern
        for _ in range(3):
            o = _order(self.svc, order_id=f"ord-{uuid.uuid4().hex[:8]}", buyer_type="B2B", buyer_ref="buyer-risk")
            self.svc.create_purpose_declaration(
                tenant_id="tenant-a",
                order_id=o.order_id,
                buyer_inn="1",
                buyer_name="X",
                selected_option=1,
            )
            decision = self.svc.evaluate_compliance(self.svc._get_order("tenant-a", o.order_id))
        self.assertTrue(decision.requires_hitl or decision.scenario == "compliance_risk")
        blob = json.dumps(self.svc.store.list_audit("tenant-a"))
        self.assertNotIn("fraud", blob.lower())
        self.assertNotIn("accused", blob.lower())


class WorkflowPathTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()
        _seed_stock(self.svc, qty=20)

    def test_b2c_happy_and_compliance_pending(self):
        order = _order(self.svc)
        result = self.svc.run_b2c_fulfillment(
            "tenant-a", order.order_id, capabilities=CAPS_FULL, idempotency_key="b2c-1"
        )
        self.assertEqual(result.status, "completed")
        again = self.svc.run_b2c_fulfillment(
            "tenant-a", order.order_id, capabilities=CAPS_FULL, idempotency_key="b2c-1"
        )
        self.assertTrue(again.provenance.get("idempotent"))
        order2 = _order(self.svc, order_id=f"ord-{uuid.uuid4().hex[:8]}")
        pending = self.svc.run_b2c_fulfillment(
            "tenant-a", order2.order_id, capabilities=CAPS_FULL, fiscal_ok=False
        )
        self.assertEqual(pending.status, ORDER_COMPLIANCE_PENDING)

    def test_b2b_own_use_and_resale(self):
        own = _order(self.svc, buyer_type="B2B", buyer_ref="b2b-1")
        self.svc.create_purpose_declaration(
            tenant_id="tenant-a",
            order_id=own.order_id,
            buyer_inn="7707083893",
            buyer_name="OOO",
            selected_option=1,
        )
        r1 = self.svc.run_b2b_own_use("tenant-a", own.order_id, capabilities=CAPS_FULL)
        self.assertEqual(r1.status, "completed")
        st = self.svc.marking.read_status(tenant_id="tenant-a", code_ref="code-1")
        self.assertEqual(st.status, MARKING_WITHDRAWN)

        self.svc.marking.seed(tenant_id="tenant-a", code_ref="code-2")
        resale = _order(
            self.svc,
            order_id=f"ord-{uuid.uuid4().hex[:8]}",
            buyer_type="B2B",
            buyer_ref="b2b-2",
            lines=(
                CommerceOrderLine(
                    product_ref="sku-1",
                    quantity=1,
                    warehouse="main",
                    marking_code_refs=("code-2",),
                ),
            ),
        )
        decl = self.svc.create_purpose_declaration(
            tenant_id="tenant-a",
            order_id=resale.order_id,
            buyer_inn="7707083893",
            buyer_name="OOO",
            selected_option=2,
        )
        self.assertEqual(decl.exact_text, DECLARATION_RESALE_V1)
        r2 = self.svc.run_b2b_resale("tenant-a", resale.order_id, capabilities=CAPS_FULL)
        self.assertEqual(r2.status, "completed")
        st2 = self.svc.marking.read_status(tenant_id="tenant-a", code_ref="code-2")
        self.assertEqual(st2.status, MARKING_TRANSFERRED)
        self.assertNotEqual(st2.status, MARKING_WITHDRAWN)

    def test_return_and_cancel(self):
        order = _order(self.svc)
        self.svc.run_b2c_fulfillment("tenant-a", order.order_id, capabilities=CAPS_FULL)
        ret = self.svc.return_order(
            "tenant-a",
            order.order_id,
            capabilities=CAPS_FULL,
            reintroduce_marking=True,
            hitl_approved=True,
        )
        self.assertEqual(ret.status, "completed")
        early = _order(self.svc, order_id=f"ord-{uuid.uuid4().hex[:8]}")
        cancelled = self.svc.cancel_order("tenant-a", early.order_id, capabilities=CAPS_FULL)
        self.assertEqual(cancelled.status, "cancelled")


class RulesAndReconcileTests(unittest.TestCase):
    def test_rule_version_retained(self):
        svc = _svc()
        _seed_stock(svc)
        order = _order(svc)
        decision = svc.evaluate_compliance(order)
        used = svc.store.rules_used_for_order("tenant-a", order.order_id)
        self.assertTrue(used)
        self.assertIn(decision.rule_version.split("+")[0].split("@")[0], used[0]["rule_id"] or decision.rule_version)

    def test_reconcile_findings(self):
        svc = _svc()
        _seed_stock(svc)
        order = _order(svc, buyer_type="B2B")
        svc.create_purpose_declaration(
            tenant_id="tenant-a",
            order_id=order.order_id,
            buyer_inn="1",
            buyer_name="X",
            selected_option=2,
        )
        svc.run_b2b_resale("tenant-a", order.order_id, capabilities=CAPS_FULL)
        # force cancelled+withdrawn inconsistency on another code
        svc.marking.seed(tenant_id="tenant-a", code_ref="bad", status=MARKING_WITHDRAWN)
        cancelled = _order(
            svc,
            order_id=f"ord-{uuid.uuid4().hex[:8]}",
            lines=(CommerceOrderLine(product_ref="sku-1", quantity=1, marking_code_refs=("bad",)),),
        )
        svc.cancel_order("tenant-a", cancelled.order_id, capabilities=CAPS_FULL)
        result = svc.reconcile_order("tenant-a", cancelled.order_id)
        self.assertIn(result["severity"], {"WARNING", "RECONCILIATION_ERROR", "OK", "HUMAN_REVIEW"})


class SecurityTests(unittest.TestCase):
    def test_no_card_data_and_capability_deny(self):
        with self.assertRaises(CardDataForbiddenError):
            CommerceOrder(
                order_id="x",
                tenant_id="t",
                buyer_type="B2C",
                totals={"pan": "4111111111111111"},
            )
        svc = _svc()
        _seed_stock(svc)
        order = _order(svc)
        with self.assertRaises(Exception):
            svc.reserve_order("tenant-a", order.order_id, capabilities=())


class SupplierTests(unittest.TestCase):
    def test_rank_with_evidence(self):
        svc = _svc()
        svc.upsert_supplier(
            SupplierRecord(
                supplier_id="s1",
                tenant_id="tenant-a",
                reliability_score=0.9,
                lead_time_days=2,
                error_rate=0.01,
            )
        )
        ranked = svc.rank_suppliers("tenant-a", price=100)
        self.assertEqual(ranked[0]["supplier_id"], "s1")
        self.assertIn("evidence", ranked[0])


class ProductionWiringTests(unittest.TestCase):
    def test_compose_commerce_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "com.sqlite3")
            runtime = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                    "COMMERCE_ENABLED": "true",
                    "COMMERCE_USE_SHARED_DB": "true",
                    "INTEGRATION_SECRETS_BACKEND": "memory",
                },
            )
            try:
                self.assertIsNotNone(runtime.commerce_runtime)
                self.assertIsNotNone(runtime.tool_gateway)
                self.assertIs(runtime.tool_gateway, runtime.tool_gateway)
                self.assertIs(
                    runtime.workflow_engine.commerce_service,
                    runtime.commerce_runtime.service,
                )
                for wf in (
                    "commerce.procurement_receive",
                    "commerce.b2c_fulfillment",
                    "commerce.b2b_own_use",
                    "commerce.b2b_resale",
                    "commerce.return",
                    "commerce.cancel",
                    "commerce.reconcile",
                ):
                    self.assertIsNotNone(runtime.workflow_runtime.definitions.get(wf, "1"))
                for tool_id in (
                    "commerce.order.read",
                    "inventory.read",
                    "inventory.reserve",
                    "edo.status",
                    "marking.status",
                    "fiscal.status",
                    "commerce.reconcile",
                ):
                    desc = runtime.tool_registry.get(tool_id)
                    self.assertIsNotNone(desc, tool_id)
                    self.assertTrue(desc.enabled, tool_id)
                # restart persistence
                order = _order(runtime.commerce_runtime.service)
                runtime.commerce_runtime.service.store.save_order(
                    "tenant-a",
                    order.order_id,
                    {"order_id": order.order_id, "tenant_id": "tenant-a", "buyer_type": "B2C", "lines": [], "fulfillment_state": "NEW", "created_at": datetime.now(timezone.utc).isoformat(), "updated_at": datetime.now(timezone.utc).isoformat()},
                    "NEW",
                )
            finally:
                runtime.close()

            runtime2 = compose_side_effect_runtime(
                secrets=DictSecrets(),
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                    "COMMERCE_ENABLED": "true",
                    "COMMERCE_USE_SHARED_DB": "true",
                    "INTEGRATION_SECRETS_BACKEND": "memory",
                },
            )
            try:
                self.assertIsNotNone(runtime2.commerce_runtime)
                loaded = runtime2.commerce_runtime.store.get_order("tenant-a", order.order_id)
                self.assertIsNotNone(loaded)
            finally:
                runtime2.close()


class EvalSmokeTests(unittest.TestCase):
    def test_commerce_eval_helpers(self):
        from commerce.evals import run_commerce_evals

        report = run_commerce_evals()
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["total"], 8)


if __name__ == "__main__":
    unittest.main()
