"""Big Block C — Commerce operations safety (17–19) targeted tests. Offline only."""

from __future__ import annotations

import io
import unittest
from decimal import Decimal

from PIL import Image

from commerce.capabilities import CAP_PRICING_WRITE, CAP_STOCK_WRITE
from commerce_ops.errors import (
    ACCESS_DENIED,
    CAPABILITY_DENIED,
    EMPTY_SELECTION,
    LIVE_FORBIDDEN,
    NOOP_PRICE,
    STALE_ECONOMICS,
    STALE_APPROVAL,
    UNSAFE_PRICE,
    UNKNOWN_DISCOUNT_OWNERSHIP,
    CommerceOpsError,
)
from commerce_ops.protection import evaluate_proposed_price
from commerce_ops.service import CommerceOpsService
from data_intel.economics import (
    CHANNEL_SITE,
    CHANNEL_WB,
    DISCOUNT_PLATFORM,
    DISCOUNT_SELLER,
    DISCOUNT_UNKNOWN,
    EconomicsInput,
    PROV_CONFIGURED,
    PROV_USER,
)
from governed_publish.contracts import MODE_LIVE, STATUS_BLOCKED, STATUS_EXECUTED_FIXTURE, TARGET_OZON, TARGET_WILDBERRIES
from governed_publish.selection import select_packages
from governed_publish.service import GovernedPublishService
from product_content.contracts import ROLE_MAIN
from product_content.service import ProductContentService
from product_content.store import ProductContentStore


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (8, 8, 8)).save(buf, format="PNG")
    return buf.getvalue()


def _econ(*, channel=CHANNEL_SITE, purchase="100", commission="0", advertising="0", logistics="0", **kw):
    return EconomicsInput(
        sku=kw.get("sku", "PH-1"),
        channel=channel,
        purchase_price=Decimal(purchase),
        purchase_price_prov=PROV_CONFIGURED,
        selling_price=Decimal(kw.get("selling", "200")),
        selling_price_prov=PROV_USER,
        commission_rate=Decimal(commission),
        commission_prov=PROV_CONFIGURED,
        advertising_cost=Decimal(advertising),
        advertising_prov=PROV_CONFIGURED,
        logistics_cost=Decimal(logistics),
        logistics_prov=PROV_CONFIGURED,
        discount_ownership=kw.get("ownership", DISCOUNT_UNKNOWN),
        discount_amount=kw.get("discount_amount"),
        marketplace_subsidy=kw.get("marketplace_subsidy"),
        seller_subsidy=kw.get("seller_subsidy"),
        currency="RUB",
    )


PHONE = {
    "sku": "PH-OPS",
    "product_id": "prod-ops-ph",
    "product_name": "Ops Phone",
    "brand": "Nova",
    "model": "O",
    "category": "smartphone",
    "color": "black",
    "memory": "8 GB",
    "purchase_price": "100",
    "selling_price": "200",
    "source": "FILE_PROVIDED",
}


class BlockCTests(unittest.TestCase):
    def setUp(self):
        self.content = ProductContentService(store=ProductContentStore())
        self.pub = GovernedPublishService()
        self.ops = CommerceOpsService()
        self.price_caps = {CAP_PRICING_WRITE}
        self.stock_caps = {CAP_STOCK_WRITE}

    def _pkg(self, tenant="t-a", **kw):
        return self.content.build_package(
            {**PHONE, **kw},
            tenant_id=tenant,
            media=[{"asset_id": "m", "role": ROLE_MAIN, "data": _png()}],
        )

    def test_price_e2e_and_protection(self):
        pkg = self._pkg()
        site = _econ(channel=CHANNEL_SITE, commission="0")
        wb = _econ(channel=CHANNEL_WB, commission="0.15")
        safe = evaluate_proposed_price(site, proposed=Decimal("200"))
        self.assertEqual(safe["decision"], "ALLOW")
        self.assertIsNotNone(safe["minimum_price"])
        self.assertIn("Not net profit", safe["contribution_note"])
        below = evaluate_proposed_price(wb, proposed=Decimal("120"))
        self.assertEqual(below["decision"], "DENY")
        incomplete = evaluate_proposed_price(EconomicsInput(channel=CHANNEL_SITE, currency="RUB"), proposed=Decimal("200"))
        self.assertEqual(incomplete["decision"], "REQUIRE_REVIEW")
        self.assertIsNone(incomplete["minimum_price"])
        unk = evaluate_proposed_price(
            _econ(ownership=DISCOUNT_UNKNOWN, discount_amount=Decimal("50")),
            proposed=Decimal("200"),
        )
        self.assertEqual(unk["code"], UNKNOWN_DISCOUNT_OWNERSHIP)
        seller = evaluate_proposed_price(
            _econ(ownership=DISCOUNT_SELLER, discount_amount=Decimal("90"), commission="0"),
            proposed=Decimal("120"),
        )
        self.assertIn(seller["decision"], {"DENY", "WARN", "ALLOW", "REQUIRE_REVIEW"})
        plat = evaluate_proposed_price(
            _econ(ownership=DISCOUNT_PLATFORM, marketplace_subsidy=Decimal("20")),
            proposed=Decimal("200"),
        )
        self.assertEqual(plat["discount_note"], "platform_funded")
        self.assertEqual(plat["marketplace_subsidy"], "20")
        site_120 = evaluate_proposed_price(site, proposed=Decimal("120"))
        wb_120 = evaluate_proposed_price(wb, proposed=Decimal("120"))
        self.assertNotEqual(site_120["decision"], wb_120["decision"])

        plan = self.ops.plan_price(
            tenant_id="t-a",
            package=pkg,
            target="SITE",
            proposed_price=Decimal("200"),
            current_price=Decimal("180"),
            economics=site,
            requested_by="agent",
            reason="list update",
            provenance=PROV_USER,
        )
        self.assertEqual(plan.action, "INCREASE")
        self.assertEqual(plan.payload["economics_decision"], "ALLOW")
        self.ops.approve(plan.plan_id, tenant_id="t-a", actor="reviewer")
        rec = self.ops.execute(
            plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.price_caps, package=pkg, economics=site, proposed_price=Decimal("200")
        )
        self.assertEqual(rec.status, STATUS_EXECUTED_FIXTURE)
        self.assertTrue(rec.fixture_reference.startswith("fixture:price:"))
        self.assertFalse(rec.published_live)
        replay = self.ops.execute(
            plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.price_caps, package=pkg, economics=site, proposed_price=Decimal("200")
        )
        self.assertEqual(replay.status, "ALREADY_EXECUTED")

        noop = self.ops.plan_price(
            tenant_id="t-a",
            package=pkg,
            target="SITE",
            proposed_price=Decimal("200"),
            current_price=Decimal("200"),
            economics=site,
            requested_by="agent",
        )
        self.assertEqual(noop.action, "UNCHANGED")
        with self.assertRaises(CommerceOpsError) as nctx:
            self.ops.execute(noop.plan_id, tenant_id="t-a", actor="r", capabilities=self.price_caps)
        self.assertEqual(nctx.exception.code, NOOP_PRICE)

        denied = self.ops.plan_price(
            tenant_id="t-a",
            package=pkg,
            target=TARGET_WILDBERRIES,
            proposed_price=Decimal("120"),
            current_price=Decimal("200"),
            economics=wb,
            requested_by="agent",
        )
        self.assertEqual(denied.status, STATUS_BLOCKED)
        self.assertIn(UNSAFE_PRICE, denied.issues)

        rej = self.ops.plan_price(
            tenant_id="t-a",
            package=pkg,
            target="SITE",
            proposed_price=Decimal("210"),
            current_price=Decimal("180"),
            economics=site,
            requested_by="agent",
        )
        self.ops.reject(rej.plan_id, tenant_id="t-a", actor="reviewer")
        with self.assertRaises(CommerceOpsError):
            self.ops.execute(rej.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.price_caps, package=pkg)

        stale_p = self.ops.plan_price(
            tenant_id="t-a",
            package=pkg,
            target="SITE",
            proposed_price=Decimal("220"),
            current_price=Decimal("180"),
            economics=site,
            requested_by="agent",
        )
        self.ops.approve(stale_p.plan_id, tenant_id="t-a", actor="reviewer")
        other = _econ(channel=CHANNEL_SITE, commission="0", purchase="150")
        with self.assertRaises(CommerceOpsError) as sctx:
            self.ops.execute(
                stale_p.plan_id,
                tenant_id="t-a",
                actor="reviewer",
                capabilities=self.price_caps,
                package=pkg,
                economics=other,
                proposed_price=Decimal("220"),
            )
        self.assertEqual(sctx.exception.code, STALE_ECONOMICS)

        with self.assertRaises(CommerceOpsError) as live:
            self.ops.plan_price(
                tenant_id="t-a",
                package=pkg,
                target="SITE",
                proposed_price=Decimal("200"),
                current_price=Decimal("180"),
                economics=site,
                requested_by="agent",
                mode=MODE_LIVE,
            )
        self.assertEqual(live.exception.code, LIVE_FORBIDDEN)

        cap_p = self.ops.plan_price(
            tenant_id="t-a",
            package=pkg,
            target="SITE",
            proposed_price=Decimal("230"),
            current_price=Decimal("180"),
            economics=site,
            requested_by="agent",
        )
        self.ops.approve(cap_p.plan_id, tenant_id="t-a", actor="reviewer")
        with self.assertRaises(CommerceOpsError) as cctx:
            self.ops.execute(cap_p.plan_id, tenant_id="t-a", actor="reviewer", capabilities=set(), package=pkg)
        self.assertEqual(cctx.exception.code, CAPABILITY_DENIED)

        other_pkg = self._pkg(tenant="t-b", sku="PH-B", product_id="pb")
        plan_b = self.ops.plan_price(
            tenant_id="t-b",
            package=other_pkg,
            target="SITE",
            proposed_price=Decimal("200"),
            current_price=Decimal("180"),
            economics=site,
            requested_by="agent",
        )
        with self.assertRaises(CommerceOpsError) as tctx:
            self.ops.store.get_plan(plan_b.plan_id, tenant_id="t-a")
        self.assertEqual(tctx.exception.code, ACCESS_DENIED)

    def test_stock_e2e_selection_isolation(self):
        pkg = self._pkg()
        pos = self.ops.plan_stock(
            tenant_id="t-a", package=pkg, target="SITE", available=Decimal("10"), requested_by="agent", safety_stock=Decimal("2")
        )
        self.assertEqual(pos.payload["published"], "8")
        self.assertEqual(pos.payload["kind_qty"], "KNOWN_POSITIVE")
        self.ops.approve(pos.plan_id, tenant_id="t-a", actor="reviewer")
        rec = self.ops.execute(pos.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.stock_caps, package=pkg, kind="stock")
        self.assertEqual(rec.status, STATUS_EXECUTED_FIXTURE)
        self.assertTrue(rec.fixture_reference.startswith("fixture:stock:"))
        replay = self.ops.execute(pos.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.stock_caps, package=pkg, kind="stock")
        self.assertEqual(replay.status, "ALREADY_EXECUTED")

        zero = self.ops.plan_stock(tenant_id="t-a", package=pkg, target="SITE", available=Decimal("0"), requested_by="agent")
        self.assertEqual(zero.payload["kind_qty"], "KNOWN_ZERO")
        self.assertEqual(zero.payload["published"], "0")

        unk = self.ops.plan_stock(tenant_id="t-a", package=pkg, target="SITE", available=None, requested_by="agent")
        self.assertEqual(unk.status, STATUS_BLOCKED)
        self.assertIsNone(unk.payload["published"])

        stale = self.ops.plan_stock(
            tenant_id="t-a", package=pkg, target="SITE", available=Decimal("5"), requested_by="agent", freshness="STALE"
        )
        self.assertEqual(stale.status, STATUS_BLOCKED)

        neg = self.ops.plan_stock(tenant_id="t-a", package=pkg, target="SITE", available=Decimal("-1"), requested_by="agent")
        self.assertEqual(neg.status, STATUS_BLOCKED)

        floor = self.ops.plan_stock(
            tenant_id="t-a", package=pkg, target="SITE", available=Decimal("3"), requested_by="agent", safety_stock=Decimal("10")
        )
        self.assertEqual(floor.payload["published"], "0")

        with self.assertRaises(CommerceOpsError) as empty:
            select_packages([pkg], tenant_id="t-a")
        self.assertEqual(empty.exception.code, EMPTY_SELECTION)

        cloth = self.content.build_package(
            {**PHONE, "sku": "CL-1", "product_id": "cloth", "category": "clothing", "product_name": "Tee"},
            tenant_id="t-a",
            media=[{"asset_id": "m", "role": ROLE_MAIN, "data": _png()}],
        )
        miss = self.ops.plan_stock(tenant_id="t-a", package=cloth, target=TARGET_WILDBERRIES, available=Decimal("4"), requested_by="agent")
        self.assertEqual(miss.status, STATUS_BLOCKED)

        amb = self.ops.plan_stock(
            tenant_id="t-a", package=pkg, target=TARGET_OZON, available=Decimal("4"), requested_by="agent", ambiguous={"smartphone"}
        )
        self.assertEqual(amb.status, STATUS_BLOCKED)

        extra = self._pkg(sku="PH-2", product_id="p2")
        batch = self.ops.sync_stock_batch(
            [pkg, extra],
            tenant_id="t-a",
            target="SITE",
            quantities={"prod-ops-ph": Decimal("9"), "p2": Decimal("1")},
            requested_by="agent",
            actor="reviewer",
            capabilities=self.stock_caps,
            product_ids=("prod-ops-ph", "p2"),
            exclude_ids=("p2",),
            execute=True,
        )
        ids = [r["product_id"] for r in batch["results"]]
        self.assertIn("prod-ops-ph", ids)
        self.assertNotIn("p2", ids)
        self.assertFalse(batch["published_live"])

        with self.assertRaises(CommerceOpsError):
            self.ops.plan_stock(tenant_id="t-a", package=pkg, target="SITE", available=Decimal("1"), requested_by="agent", mode=MODE_LIVE)

        other = self._pkg(tenant="t-b", sku="X", product_id="xb")
        pb = self.ops.plan_stock(tenant_id="t-b", package=other, target="SITE", available=Decimal("2"), requested_by="agent")
        with self.assertRaises(CommerceOpsError):
            self.ops.store.get_plan(pb.plan_id, tenant_id="t-a")

        stale_ok = self.ops.plan_stock(
            tenant_id="t-a", package=pkg, target="SITE", available=Decimal("11"), requested_by="agent"
        )
        self.ops.approve(stale_ok.plan_id, tenant_id="t-a", actor="reviewer")
        with self.assertRaises(CommerceOpsError) as st:
            self.ops.execute(
                stale_ok.plan_id,
                tenant_id="t-a",
                actor="reviewer",
                capabilities=self.stock_caps,
                package=pkg,
                kind="stock",
                snapshot={"version": "other-snap"},
            )
        self.assertEqual(st.exception.code, STALE_APPROVAL)

    def test_cross_block_e2e(self):
        pkg = self._pkg()
        self.assertTrue(pkg.card.economics_reference.get("engine"))
        mp = self.pub.plan_marketplace(pkg, tenant_id="t-a", requested_by="agent", target=TARGET_WILDBERRIES)
        self.assertIn(mp.status, {"APPROVAL_REQUIRED", "BLOCKED"})
        econ = _econ(channel=CHANNEL_SITE, commission="0")
        plan = self.ops.plan_price(
            tenant_id="t-a",
            package=pkg,
            target="SITE",
            proposed_price=Decimal("200"),
            current_price=Decimal("180"),
            economics=econ,
            requested_by="agent",
            reason="cross-block",
        )
        self.ops.approve(plan.plan_id, tenant_id="t-a", actor="reviewer")
        rec = self.ops.execute(
            plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.price_caps, package=pkg, economics=econ, proposed_price=Decimal("200")
        )
        stock = self.ops.plan_stock(tenant_id="t-a", package=pkg, target="SITE", available=Decimal("6"), requested_by="agent", safety_stock=Decimal("1"))
        self.ops.approve(stock.plan_id, tenant_id="t-a", actor="reviewer")
        srec = self.ops.execute(stock.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.stock_caps, package=pkg, kind="stock")
        self.assertEqual(rec.status, STATUS_EXECUTED_FIXTURE)
        self.assertEqual(srec.status, STATUS_EXECUTED_FIXTURE)
        self.assertTrue(any(e["event"] == "EXECUTED_FIXTURE" for e in self.ops.store.list_audit(tenant_id="t-a")))
        back = self.ops.store.get_receipt(rec.receipt_id, tenant_id="t-a")
        self.assertEqual(back.content_version, pkg.version)


if __name__ == "__main__":
    unittest.main()
