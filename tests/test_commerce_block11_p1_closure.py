"""Block 11 P1 closure patch tests."""

from __future__ import annotations

import unittest
import uuid
from decimal import Decimal

from commerce.capabilities import (
    CAP_CATALOG_READ,
    CAP_CATALOG_WRITE,
    CAP_PRICING_WRITE,
    CAP_STOCK_WRITE,
)
from commerce.product_platform.errors import (
    COMMERCE_ACCESS_DENIED,
    COMMERCE_ORDER_EVENT_IDEMPOTENT,
    COMMERCE_ORDER_STALE_EVENT,
    COMMERCE_STOCK_STALE,
    CommerceBatchRequired,
    ProductPlatformError,
)
from commerce.product_platform.policy import MAX_SYNC_REPRICE_COUNT
from commerce.product_platform.side_effect import (
    COMMERCE_WRITE_TOOLS,
    register_commerce_platform_side_effects,
)
from commerce.product_platform.service import ProductPlatformService
from commerce.store import CommerceStore
from side_effects.executor import SideEffectExecutor
from side_effects.models import SideEffectExecutionContext
from side_effects.registry import SideEffectAdapterRegistry
from tests.side_effect_fixtures import T0, caps, eval_kwargs, se_action
from tools.adapters import descriptor_from_side_effect
from tools.gateway import ToolGateway
from tools.models import TOOL_STATUS_SUCCEEDED, TOOL_TRUST_INTERNAL_SAFE, ToolRequest
from tools.platform.descriptors import (
    TOOL_COMMERCE_CMS_CREATE,
    TOOL_COMMERCE_CMS_STOCK_UPDATE,
    TOOL_COMMERCE_PRICE_APPLY,
)
from tools.registry import ToolRegistry
from workflow.engine import WorkflowEngine


def _svc() -> ProductPlatformService:
    return ProductPlatformService(
        store=CommerceStore(path="file:commerce_p1?mode=memory&cache=shared")
    )


def _commerce_gateway(*, tenant_id: str = "tenant-a"):
    engine = WorkflowEngine()
    workflow_id = engine.create("commerce-p1", tenant_id=tenant_id)
    engine.state_manager.plan(workflow_id)
    engine.state_manager.start(workflow_id)
    svc = _svc()
    pp = svc
    from commerce.product_platform.tools import ProductPlatformToolAdapter

    platform_adapter = ProductPlatformToolAdapter(svc, enabled=True)
    se_reg = SideEffectAdapterRegistry()
    registered = register_commerce_platform_side_effects(
        se_reg,
        platform_adapter,
        trust_level=TOOL_TRUST_INTERNAL_SAFE,
        reversible=True,
    )
    gate = engine._gate()
    executor = SideEffectExecutor(se_reg, gate=gate)
    registry = ToolRegistry()
    for spec in COMMERCE_WRITE_TOOLS:
        if spec["tool_id"] not in registered:
            continue
        adapter = se_reg.get(spec["tool_id"])
        registry.register(
            descriptor_from_side_effect(
                adapter.descriptor,
                name=spec["tool_id"],
                version="1.0.0",
                enabled=True,
                idempotency_required=True,
            ),
            adapter=adapter,
        )
    gateway = ToolGateway(
        registry=registry,
        side_effect_executor=executor,
        gate=gate,
        register_search=False,
    )
    return gateway, executor, engine, svc, workflow_id, registered


class CmsStockUpdateTests(unittest.TestCase):
    def test_trusted_inventory_to_cms(self):
        svc = _svc()
        v = svc.create_product_version(tenant_id="tenant-a", title="Stock", sku="STK-1")
        svc.observe_stock(tenant_id="tenant-a", product_id=v.product_id, location_id="main", on_hand=Decimal("25"))
        svc.cms_create_product(
            tenant_id="tenant-a",
            product_id=v.product_id,
            version_id=v.version_id,
            idempotency_key="cms-stk-1",
            capabilities=(CAP_CATALOG_WRITE,),
        )
        result = svc.cms_update_stock(
            tenant_id="tenant-a",
            product_id=v.product_id,
            location_id="main",
            idempotency_key="stock-sync-1",
            capabilities=(CAP_STOCK_WRITE,),
        )
        self.assertEqual(result.status, "updated")
        ext = svc.cms.get_product(tenant_id="tenant-a", external_id=result.external_id)
        self.assertEqual(Decimal(ext["stock"]), Decimal("25"))

    def test_raw_stock_override_denied(self):
        svc = _svc()
        from commerce.product_platform.tools import ProductPlatformToolAdapter

        adapter = ProductPlatformToolAdapter(svc, enabled=True)
        import asyncio

        req = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id="wf",
            task_id="t",
            tool_id="commerce.cms.stock.update",
            operation="cms_update_stock",
            arguments={"product_id": "p1", "stock": 999999},
            requested_capabilities=(CAP_STOCK_WRITE,),
            tenant_id="tenant-a",
        )
        with self.assertRaises(Exception):
            asyncio.get_event_loop().run_until_complete(adapter.execute_write(req, {}))

    def test_stale_inventory_version_denied(self):
        svc = _svc()
        v = svc.create_product_version(tenant_id="tenant-a", title="Stale", sku="STK-2")
        svc.observe_stock(tenant_id="tenant-a", product_id=v.product_id, location_id="main", on_hand=Decimal("5"))
        svc.cms_create_product(
            tenant_id="tenant-a",
            product_id=v.product_id,
            version_id=v.version_id,
            idempotency_key="cms-stk-2",
            capabilities=(CAP_CATALOG_WRITE,),
        )
        with self.assertRaises(ProductPlatformError) as ctx:
            svc.cms_update_stock(
                tenant_id="tenant-a",
                product_id=v.product_id,
                idempotency_key="stale-inv",
                capabilities=(CAP_STOCK_WRITE,),
                expected_inventory_version=999,
            )
        self.assertEqual(ctx.exception.code, COMMERCE_STOCK_STALE)

    def test_missing_capability_denied(self):
        svc = _svc()
        v = svc.create_product_version(tenant_id="tenant-a", title="Cap", sku="STK-3")
        with self.assertRaises(ProductPlatformError) as ctx:
            svc.cms_update_stock(
                tenant_id="tenant-a",
                product_id=v.product_id,
                idempotency_key="cap-1",
                capabilities=(),
            )
        self.assertEqual(ctx.exception.code, COMMERCE_ACCESS_DENIED)


class SideEffectRegistrationTests(unittest.TestCase):
    def test_registry_resolves_commerce_writes(self):
        _, _, _, _, _, registered = _commerce_gateway()
        required = {
            "commerce.price.apply",
            "commerce.cms.product.create",
            "commerce.cms.stock.update",
        }
        for tool_id in required:
            self.assertIn(tool_id, registered)

    def test_unknown_write_fails_closed(self):
        se_reg = SideEffectAdapterRegistry()
        with self.assertRaises(Exception):
            se_reg.require("commerce.unknown.write")


class ToolGatewayE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_price_apply_via_gateway(self):
        gateway, executor, engine, svc, workflow_id, _ = _commerce_gateway()
        v = svc.create_product_version(tenant_id="tenant-a", title="GW", sku="GW-1")
        svc.repo.set_price("tenant-a", v.product_id, "RUB", Decimal("100"))
        svc.set_trusted_cost(tenant_id="tenant-a", product_id=v.product_id, amount=Decimal("70"))
        svc.cms_create_product(
            tenant_id="tenant-a",
            product_id=v.product_id,
            version_id=v.version_id,
            idempotency_key="gw-cms",
            capabilities=(CAP_CATALOG_WRITE,),
        )
        decision = svc.decide_price(tenant_id="tenant-a", product_id=v.product_id, proposed_amount=Decimal("105"))
        req = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            task_id="t",
            tool_id=TOOL_COMMERCE_PRICE_APPLY,
            operation="apply_price",
            arguments={"decision_id": decision.decision_id},
            requested_capabilities=(CAP_PRICING_WRITE,),
            idempotency_key="gw-price-1",
            tenant_id="tenant-a",
        )
        capset = caps(CAP_PRICING_WRITE)
        result = await gateway.invoke(
            req,
            capabilities=capset,
            gate=engine._gate(),
            executor=executor,
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs(capabilities=(CAP_PRICING_WRITE,)),
            now=T0,
        )
        self.assertTrue(result.success, result.error_code)
        self.assertEqual(result.status, TOOL_STATUS_SUCCEEDED)

    async def test_cms_create_via_gateway(self):
        gateway, executor, engine, svc, workflow_id, _ = _commerce_gateway()
        v = svc.create_product_version(tenant_id="tenant-a", title="GWC", sku="GWC-1")
        req = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            task_id="t",
            tool_id=TOOL_COMMERCE_CMS_CREATE,
            operation="cms_create",
            arguments={"product_id": v.product_id, "version_id": v.version_id},
            requested_capabilities=(CAP_CATALOG_WRITE,),
            idempotency_key="gw-create-1",
            tenant_id="tenant-a",
        )
        capset = caps(CAP_CATALOG_WRITE)
        result = await gateway.invoke(
            req,
            capabilities=capset,
            gate=engine._gate(),
            executor=executor,
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs(capabilities=(CAP_CATALOG_WRITE,)),
            now=T0,
        )
        self.assertTrue(result.success, result.error_code)

    async def test_cms_stock_via_gateway(self):
        gateway, executor, engine, svc, workflow_id, _ = _commerce_gateway()
        v = svc.create_product_version(tenant_id="tenant-a", title="GWS", sku="GWS-1")
        svc.observe_stock(tenant_id="tenant-a", product_id=v.product_id, location_id="main", on_hand=Decimal("8"))
        svc.cms_create_product(
            tenant_id="tenant-a",
            product_id=v.product_id,
            version_id=v.version_id,
            idempotency_key="gw-st-create",
            capabilities=(CAP_CATALOG_WRITE,),
        )
        req = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            task_id="t",
            tool_id=TOOL_COMMERCE_CMS_STOCK_UPDATE,
            operation="cms_update_stock",
            arguments={"product_id": v.product_id, "location_id": "main"},
            requested_capabilities=(CAP_STOCK_WRITE,),
            idempotency_key="gw-stock-1",
            tenant_id="tenant-a",
        )
        capset = caps(CAP_STOCK_WRITE)
        result = await gateway.invoke(
            req,
            capabilities=capset,
            gate=engine._gate(),
            executor=executor,
            state_manager=engine.state_manager,
            evaluate_kwargs=eval_kwargs(capabilities=(CAP_STOCK_WRITE,)),
            now=T0,
        )
        self.assertTrue(result.success, result.error_code)


class BulkRepricingTests(unittest.TestCase):
    def test_durable_bulk_apply_checkpoint(self):
        svc = _svc()
        decision_ids = []
        for i in range(3):
            v = svc.create_product_version(tenant_id="tenant-a", title=f"B{i}", sku=f"BULK-{i}")
            svc.repo.set_price("tenant-a", v.product_id, "RUB", Decimal("100"))
            svc.set_trusted_cost(tenant_id="tenant-a", product_id=v.product_id, amount=Decimal("70"))
            d = svc.decide_price(tenant_id="tenant-a", product_id=v.product_id, proposed_amount=Decimal("105"))
            decision_ids.append(d.decision_id)
        first = svc.start_bulk_reprice_apply(
            tenant_id="tenant-a", decision_ids=decision_ids, bulk=True, job_id="bulk-job-1"
        )
        self.assertIn(first["status"], {"partial", "completed"})
        job = svc.repo.get_commerce_job("tenant-a", "bulk-job-1")
        self.assertIsNotNone(job)
        self.assertGreater(job["checkpoint"], 0)

    def test_sync_gate_blocks_large_reprice(self):
        svc = _svc()
        ids = [str(uuid.uuid4()) for _ in range(MAX_SYNC_REPRICE_COUNT + 1)]
        with self.assertRaises(CommerceBatchRequired):
            svc.start_bulk_reprice_apply(tenant_id="tenant-a", decision_ids=ids, bulk=False)


class CmsBulkSyncTests(unittest.TestCase):
    def test_durable_sync_checkpoint_no_duplicate_create(self):
        svc = _svc()
        specs = []
        for i in range(4):
            v = svc.create_product_version(tenant_id="tenant-a", title=f"Sync{i}", sku=f"SYNC-{i}")
            specs.append({"product_id": v.product_id, "version_id": v.version_id})
        first = svc.start_cms_bulk_sync(
            tenant_id="tenant-a", product_specs=specs, bulk=True, job_id="cms-sync-1"
        )
        job = svc.repo.get_commerce_job("tenant-a", "cms-sync-1")
        self.assertIsNotNone(job)
        self.assertGreaterEqual(job["checkpoint"], 1)
        second = svc.start_cms_bulk_sync(
            tenant_id="tenant-a", product_specs=specs, bulk=True, job_id="cms-sync-1"
        )
        self.assertGreaterEqual(second["checkpoint"], first["checkpoint"])


class OrderSequencingTests(unittest.TestCase):
    def test_seq_11_wins_over_10(self):
        svc = _svc()
        order = svc.ingest_order(
            tenant_id="tenant-a",
            external_ref="seq-1",
            source="web",
            items=[{"sku": "S", "quantity": "1", "unit_price": "10"}],
        )
        from commerce.product_platform.models import ORDER_CONFIRMED, ORDER_PROCESSING

        svc.transition_order(
            tenant_id="tenant-a",
            order_id=order.order_id,
            new_status=ORDER_CONFIRMED,
            external_event_id="e10",
            external_sequence=10,
        )
        svc.transition_order(
            tenant_id="tenant-a",
            order_id=order.order_id,
            new_status=ORDER_PROCESSING,
            external_event_id="e11",
            external_sequence=11,
        )
        row = svc.repo._conn().execute(
            "SELECT status FROM pp_platform_orders WHERE tenant_id=? AND order_id=?",
            ("tenant-a", order.order_id),
        ).fetchone()
        self.assertEqual(row["status"], ORDER_PROCESSING)

    def test_stale_seq_10_ignored_after_11(self):
        svc = _svc()
        order = svc.ingest_order(
            tenant_id="tenant-a",
            external_ref="seq-2",
            source="web",
            items=[{"sku": "S", "quantity": "1", "unit_price": "10"}],
        )
        from commerce.product_platform.models import ORDER_CONFIRMED, ORDER_PROCESSING

        svc.transition_order(
            tenant_id="tenant-a",
            order_id=order.order_id,
            new_status=ORDER_CONFIRMED,
            external_event_id="e11a",
            external_sequence=11,
        )
        svc.transition_order(
            tenant_id="tenant-a",
            order_id=order.order_id,
            new_status=ORDER_PROCESSING,
            external_event_id="e11b",
            external_sequence=12,
        )
        with self.assertRaises(ProductPlatformError) as ctx:
            svc.transition_order(
                tenant_id="tenant-a",
                order_id=order.order_id,
                new_status=ORDER_CONFIRMED,
                external_event_id="e10-stale",
                external_sequence=10,
            )
        self.assertEqual(ctx.exception.code, COMMERCE_ORDER_STALE_EVENT)

    def test_same_event_id_idempotent(self):
        svc = _svc()
        order = svc.ingest_order(
            tenant_id="tenant-a",
            external_ref="seq-3",
            source="web",
            items=[{"sku": "S", "quantity": "1", "unit_price": "10"}],
        )
        from commerce.product_platform.models import ORDER_CONFIRMED

        svc.transition_order(
            tenant_id="tenant-a",
            order_id=order.order_id,
            new_status=ORDER_CONFIRMED,
            external_event_id="same-ev",
            external_sequence=1,
        )
        with self.assertRaises(ProductPlatformError) as ctx:
            svc.transition_order(
                tenant_id="tenant-a",
                order_id=order.order_id,
                new_status=ORDER_CONFIRMED,
                external_event_id="same-ev",
                external_sequence=2,
            )
        self.assertEqual(ctx.exception.code, COMMERCE_ORDER_EVENT_IDEMPOTENT)


class CapabilityMatrixTests(unittest.TestCase):
    def test_catalog_read_cannot_write(self):
        from commerce.product_platform.tools import ProductPlatformToolAdapter

        svc = _svc()
        adapter = ProductPlatformToolAdapter(svc, enabled=True)
        import asyncio

        req = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id="wf",
            task_id="t",
            tool_id=TOOL_COMMERCE_PRICE_APPLY,
            operation="apply_price",
            arguments={"decision_id": "d1"},
            requested_capabilities=(CAP_CATALOG_READ,),
            tenant_id="tenant-a",
        )
        with self.assertRaises(Exception):
            asyncio.get_event_loop().run_until_complete(adapter.execute_write(req, {}))

    def test_pricing_write_does_not_grant_stock(self):
        svc = _svc()
        v = svc.create_product_version(tenant_id="tenant-a", title="CapM", sku="CAPM-1")
        with self.assertRaises(ProductPlatformError):
            svc.cms_update_stock(
                tenant_id="tenant-a",
                product_id=v.product_id,
                idempotency_key="capm-1",
                capabilities=(CAP_PRICING_WRITE,),
            )


if __name__ == "__main__":
    unittest.main()
