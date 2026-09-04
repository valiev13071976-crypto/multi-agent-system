"""Big Block B — Governed Commerce Publishing (15–16) targeted tests. Offline only."""

from __future__ import annotations

import io
import unittest
from dataclasses import replace

from PIL import Image

from commerce.capabilities import CAP_CATALOG_WRITE
from governed_publish.contracts import (
    DIFF_CREATE,
    DIFF_UNCHANGED,
    DIFF_UPDATE,
    MODE_LIVE,
    STATUS_ALREADY_EXECUTED,
    STATUS_APPROVAL_REQUIRED,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_EXECUTED_FIXTURE,
    STATUS_REJECTED,
    TARGET_OZON,
    TARGET_WILDBERRIES,
    TARGET_YANDEX_MARKET,
)
from governed_publish.diff import classify_diff
from governed_publish.errors import (
    PUBLISH_ACCESS_DENIED,
    PUBLISH_BLOCKED,
    PUBLISH_CAPABILITY_DENIED,
    PUBLISH_EMPTY_SELECTION,
    PUBLISH_LIVE_FORBIDDEN,
    PUBLISH_STALE,
    GovernedPublishError,
)
from governed_publish.marketplace_payload import marketplace_payload
from governed_publish.selection import select_packages
from governed_publish.service import GovernedPublishService
from product_content.contracts import ROLE_MAIN
from product_content.service import ProductContentService
from product_content.store import ProductContentStore


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), (12, 40, 90)).save(buf, format="PNG")
    return buf.getvalue()


HP = {
    "sku": "HP-PUB-1",
    "product_id": "prod-hp-pub",
    "product_name": "Quiet Buds",
    "brand": "Nova",
    "model": "QB1",
    "category": "headphones",
    "color": "blue",
    "article": "ART-HP-1",
    "source": "FILE_PROVIDED",
}

PHONE = {
    "sku": "PH-PUB-1",
    "product_id": "prod-ph-pub",
    "product_name": "Nova Phone X",
    "brand": "Nova",
    "model": "X",
    "category": "smartphone",
    "color": "black",
    "memory": "8 GB",
    "purchase_price": "95000",
    "selling_price": "120000",
    "source": "FILE_PROVIDED",
}


class BlockBTests(unittest.TestCase):
    def setUp(self):
        self.content = ProductContentService(store=ProductContentStore())
        self.pub = GovernedPublishService()
        self.caps = {CAP_CATALOG_WRITE}

    def _ready(self, tenant="t-a", **kw):
        row = {**HP, **kw}
        return self.content.build_package(row, tenant_id=tenant, media=[{"asset_id": "m1", "role": ROLE_MAIN, "data": _png()}])

    def _phone(self, tenant="t-a", **kw):
        row = {**PHONE, **kw}
        return self.content.build_package(row, tenant_id=tenant, media=[{"asset_id": "m1", "role": ROLE_MAIN, "data": _png()}])

    def test_site_e2e_create_preview_approve_fixture_receipt(self):
        pkg = self._ready()
        self.assertEqual(pkg.status, "READY")
        preview = self.pub.preview_site(pkg, tenant_id="t-a")
        self.assertEqual(preview.action, DIFF_CREATE)
        self.assertTrue(preview.payload.get("NAME") or preview.payload.get("XML_ID"))
        self.assertIn("seo_title", preview.seo_actions)
        self.assertTrue(any("include:" in m for m in preview.media_actions))
        plan = self.pub.plan_site(pkg, tenant_id="t-a", requested_by="agent")
        self.assertEqual(plan.status, STATUS_APPROVAL_REQUIRED)
        self.assertEqual(plan.mode, "FIXTURE")
        self.assertFalse(plan.published_live)
        with self.assertRaises(GovernedPublishError) as missing:
            self.pub.execute(plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.caps)
        self.assertEqual(missing.exception.code, PUBLISH_STALE)
        plan = self.pub.approve(plan.plan_id, tenant_id="t-a", actor="reviewer")
        self.assertEqual(plan.status, STATUS_APPROVED)
        rec = self.pub.execute(plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.caps, package=pkg)
        self.assertEqual(rec.status, STATUS_EXECUTED_FIXTURE)
        self.assertTrue(rec.fixture_reference.startswith("fixture:"))
        self.assertFalse(rec.published_live)
        back = self.pub.store.get_receipt(rec.receipt_id, tenant_id="t-a")
        self.assertEqual(back.content_version, pkg.version)
        replay = self.pub.execute(plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.caps, package=pkg)
        self.assertEqual(replay.status, STATUS_ALREADY_EXECUTED)
        self.assertEqual(replay.receipt_id, rec.receipt_id)
        events = {e["event"] for e in self.pub.store.list_audit(tenant_id="t-a")}
        self.assertIn("preview_generated", events)
        self.assertIn("approval_granted", events)
        self.assertIn("fixture_execution_result", events)
        self.assertIn("idempotent_replay", events)

    def test_eligibility_blocked_warnings_review(self):
        blocked = self.content.build_package(
            {**HP, "sku": "HP-BL", "product_id": "bl", "marketing_copy": "Official dealer IP68"},
            tenant_id="t-a",
        )
        plan = self.pub.plan_site(blocked, tenant_id="t-a", requested_by="agent")
        self.assertEqual(plan.status, STATUS_BLOCKED)
        with self.assertRaises(GovernedPublishError) as ctx:
            self.pub.execute(plan.plan_id, tenant_id="t-a", actor="r", capabilities=self.caps)
        self.assertEqual(ctx.exception.code, PUBLISH_BLOCKED)
        warn = self.content.build_package(
            {"sku": "HP-W", "product_id": "w", "product_name": "Buds", "category": "headphones"},
            tenant_id="t-a",
        )
        self.assertEqual(warn.status, "READY_WITH_WARNINGS")
        p = self.pub.plan_site(warn, tenant_id="t-a", requested_by="agent")
        self.assertEqual(p.status, STATUS_APPROVAL_REQUIRED)
        review = self.content.build_package(
            {"sku": "PH-R", "product_id": "rr", "product_name": "Phone", "category": "smartphone"},
            tenant_id="t-a",
        )
        self.assertEqual(review.status, "REQUIRES_REVIEW")
        pr = self.pub.plan_site(review, tenant_id="t-a", requested_by="agent")
        self.assertEqual(pr.status, STATUS_BLOCKED)

    def test_diff_update_unchanged_preserve_unknown(self):
        desired = {"NAME": "A", "sku": "1"}
        snap = {"NAME": "A", "sku": "1", "warranty": "12m"}
        entries = classify_diff(desired=desired, snapshot=snap)
        by = {e.field: e for e in entries}
        self.assertEqual(by["NAME"].classification, DIFF_UNCHANGED)
        self.assertEqual(by["warranty"].classification, DIFF_UNCHANGED)
        self.assertTrue(by["warranty"].omitted)
        changed = classify_diff(desired={"NAME": "B", "sku": "1"}, snapshot={"NAME": "A", "sku": "1"})
        self.assertTrue(any(e.classification == DIFF_UPDATE and e.field == "NAME" for e in changed))

    def test_stale_content_and_snapshot_and_reject_and_live(self):
        pkg = self._ready()
        plan = self.pub.plan_site(pkg, tenant_id="t-a", requested_by="agent")
        self.pub.approve(plan.plan_id, tenant_id="t-a", actor="reviewer")
        other = self._ready(product_name="Quiet Buds v2", sku="HP-PUB-1", product_id="prod-hp-pub")
        with self.assertRaises(GovernedPublishError) as stale:
            self.pub.execute(plan.plan_id, tenant_id="t-a", actor="reviewer", capabilities=self.caps, package=other)
        self.assertEqual(stale.exception.code, PUBLISH_STALE)
        pkg2 = self._ready(sku="HP-PUB-2", product_id="prod-hp-2")
        plan2 = self.pub.plan_site(pkg2, tenant_id="t-a", requested_by="agent", snapshot={"NAME": "old"})
        self.pub.approve(plan2.plan_id, tenant_id="t-a", actor="reviewer")
        with self.assertRaises(GovernedPublishError):
            self.pub.execute(
                plan2.plan_id,
                tenant_id="t-a",
                actor="reviewer",
                capabilities=self.caps,
                package=pkg2,
                snapshot={"NAME": "changed-after-preview"},
            )
        pkg3 = self._ready(sku="HP-PUB-3", product_id="prod-hp-3")
        plan3 = self.pub.plan_site(pkg3, tenant_id="t-a", requested_by="agent")
        self.pub.reject(plan3.plan_id, tenant_id="t-a", actor="reviewer")
        self.assertEqual(self.pub.store.get_plan(plan3.plan_id, tenant_id="t-a").status, STATUS_REJECTED)
        with self.assertRaises(GovernedPublishError) as live:
            self.pub.plan_site(pkg, tenant_id="t-a", requested_by="agent", mode=MODE_LIVE)
        self.assertEqual(live.exception.code, PUBLISH_LIVE_FORBIDDEN)
        with self.assertRaises(GovernedPublishError) as cap:
            plan4 = self.pub.plan_site(self._ready(sku="HP-4", product_id="p4"), tenant_id="t-a", requested_by="agent")
            self.pub.approve(plan4.plan_id, tenant_id="t-a", actor="reviewer")
            self.pub.execute(plan4.plan_id, tenant_id="t-a", actor="reviewer", capabilities=set())
        self.assertEqual(cap.exception.code, PUBLISH_CAPABILITY_DENIED)

    def test_marketplace_e2e_selection_partial_and_mappings(self):
        a = self._phone()
        b = self.content.build_package(
            {**HP, "sku": "HP-MISS", "product_id": "hp-miss", "category": "clothing"},
            tenant_id="t-a",
            media=[{"asset_id": "m1", "role": ROLE_MAIN, "data": _png()}],
        )
        c = self.content.build_package(
            {**HP, "sku": "HP-EX", "product_id": "hp-ex"},
            tenant_id="t-a",
            media=[{"asset_id": "m1", "role": ROLE_MAIN, "data": _png()}],
        )
        with self.assertRaises(GovernedPublishError) as empty:
            select_packages([a], tenant_id="t-a")
        self.assertEqual(empty.exception.code, PUBLISH_EMPTY_SELECTION)
        sel_sku = select_packages([a, b, c], tenant_id="t-a", skus=("PH-PUB-1",))
        self.assertEqual(sel_sku.selected, ("prod-ph-pub",))
        sel_art = select_packages([self._ready()], tenant_id="t-a", articles=("ART-HP-1",))
        self.assertTrue(sel_art.count)
        sel_cat = select_packages([a, b], tenant_id="t-a", categories=("smartphone",))
        self.assertIn("prod-ph-pub", sel_cat.selected)
        sel_ex = select_packages([a, c], tenant_id="t-a", product_ids=("prod-ph-pub", "hp-ex"), exclude_ids=("hp-ex",))
        self.assertNotIn("hp-ex", sel_ex.selected)
        wb = marketplace_payload(a, target=TARGET_WILDBERRIES)
        oz = marketplace_payload(a, target=TARGET_OZON)
        ym = marketplace_payload(a, target=TARGET_YANDEX_MARKET)
        self.assertEqual(wb["category_status"], "MAPPED")
        self.assertEqual(oz["category_status"], "MAPPED")
        self.assertEqual(ym["category_status"], "MAPPED")
        self.assertNotIn("search_volume", wb)
        clothing = marketplace_payload(b, target=TARGET_WILDBERRIES)
        self.assertEqual(clothing["category_status"], "MISSING")
        amb = marketplace_payload(a, target=TARGET_WILDBERRIES, ambiguous={"smartphone"})
        self.assertEqual(amb["category_status"], "AMBIGUOUS")
        batch = self.pub.export_batch(
            [a, b, c],
            tenant_id="t-a",
            requested_by="agent",
            actor="reviewer",
            capabilities=self.caps,
            target=TARGET_WILDBERRIES,
            product_ids=("prod-ph-pub", "hp-miss", "hp-ex"),
            exclude_ids=("hp-ex",),
            execute=True,
        )
        statuses = {r["product_id"]: r["status"] for r in batch["results"]}
        self.assertEqual(statuses["prod-ph-pub"], STATUS_EXECUTED_FIXTURE)
        self.assertEqual(statuses["hp-miss"], STATUS_BLOCKED)
        self.assertNotIn("hp-ex", statuses)
        self.assertFalse(batch["published_live"])
        self.assertTrue(batch["partial"])

    def test_economics_deny_media_and_tenant(self):
        pkg = self._phone()
        card = replace(pkg.card, economics_reference={**pkg.card.economics_reference, "decision": "DENY"})
        denied = replace(pkg, card=card)
        plan = self.pub.plan_marketplace(denied, tenant_id="t-a", requested_by="agent", target=TARGET_OZON)
        self.assertEqual(plan.status, STATUS_BLOCKED)
        self.assertIn("economics_deny", plan.issues)
        self.assertIsNone(plan.payload.get("stock"))
        invalid = self.content.build_package(
            {**HP, "sku": "HP-BADMED", "product_id": "badmed"},
            tenant_id="t-a",
            media=[{"asset_id": "z", "role": ROLE_MAIN, "data": b""}],
        )
        prev = self.pub.preview_site(invalid, tenant_id="t-a")
        self.assertTrue(any("invalid_media" in w for w in prev.warnings))
        other = self._ready(tenant="t-b", sku="HP-B", product_id="tb")
        with self.assertRaises(GovernedPublishError) as ctx:
            self.pub.preview_site(other, tenant_id="t-a")
        self.assertEqual(ctx.exception.code, PUBLISH_ACCESS_DENIED)
        plan_b = self.pub.plan_site(other, tenant_id="t-b", requested_by="agent")
        with self.assertRaises(GovernedPublishError):
            self.pub.store.get_plan(plan_b.plan_id, tenant_id="t-a")


if __name__ == "__main__":
    unittest.main()
