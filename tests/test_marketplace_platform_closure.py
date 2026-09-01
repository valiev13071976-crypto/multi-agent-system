"""Marketplace Platform — canonical closure tests (WB / Ozon / Yandex)."""

from __future__ import annotations

import unittest
from decimal import Decimal

from marketplace.economics import calculate_minimum_allowed_price, calculate_profitability
from marketplace.errors import (
    MARKETPLACE_AUTO_CORRECT_DENIED,
    MARKETPLACE_BATCH_REQUIRED,
    MARKETPLACE_CAPABILITY_UNSUPPORTED,
    MARKETPLACE_CROSS_TENANT,
    MARKETPLACE_SELECTION_REQUIRED,
    MARKETPLACE_SYNC_LOOP_TERMINATED,
    MARKETPLACE_UNAVAILABLE,
    MarketplaceError,
)
from marketplace.models import (
    MODE_AUTO_CORRECT,
    MODE_MONITOR_ONLY,
    MoneyAmount,
    MarketplaceCommissionObservation,
    MarketplaceMinPricePolicy,
    MarketplacePromotionObservation,
    PLATFORM_SCHEMA_VERSION,
    PROFIT_LOSS,
    PROFIT_UNKNOWN,
    PROMO_PLATFORM,
    PROMO_SELLER,
    PROVIDER_OZON,
    PROVIDER_WILDBERRIES,
    PROVIDER_YANDEX_MARKET,
    STOCK_DRIFT,
    STOCK_MATCHED,
)
from marketplace.selection import new_selection
from marketplace.service import MarketplacePlatformService


def _svc() -> MarketplacePlatformService:
    return MarketplacePlatformService()


def _catalog(n: int = 100) -> list[dict]:
    return [
        {
            "product_id": f"p-{i}",
            "sku_id": f"sku-{i}",
            "title": f"Item {i}",
            "category_id": "phones",
            "brand": "Acme" if i % 2 == 0 else "Other",
            "stock": str(i),
            "attributes": {"brand": "Acme", "color": "black", "weight": "200"},
            "media_refs": ["m1"],
            "content_refs": ["c1"],
        }
        for i in range(n)
    ]


def _seed_commission(svc: MarketplacePlatformService, provider: str, rate: str = "0.15") -> None:
    svc.set_commission(
        MarketplaceCommissionObservation(
            observation_id="c1",
            provider=provider,
            category="phones",
            rate=Decimal(rate),
            fixed_fee=Decimal("10"),
            source="fixture",
        )
    )


class ArchitectureTests(unittest.TestCase):
    def test_one_core_three_adapters(self):
        svc = _svc()
        matrix = svc.capability_matrix()
        self.assertEqual(set(matrix), {PROVIDER_WILDBERRIES, PROVIDER_OZON, PROVIDER_YANDEX_MARKET})
        self.assertIn("PRICE_WRITE", matrix[PROVIDER_WILDBERRIES])
        self.assertIn("PRICE_WRITE", matrix[PROVIDER_OZON])
        self.assertNotIn("PRICE_WRITE", matrix[PROVIDER_YANDEX_MARKET])
        self.assertIn("REVIEW_REPLY", matrix[PROVIDER_WILDBERRIES])
        self.assertNotIn("REVIEW_REPLY", matrix[PROVIDER_OZON])
        self.assertEqual(PLATFORM_SCHEMA_VERSION, "1.0.0")
        for p in matrix:
            self.assertFalse(svc.health(p)["live"])


class CapabilityFailClosedTests(unittest.TestCase):
    def test_yandex_price_write_fails(self):
        svc = _svc()
        with self.assertRaises(MarketplaceError) as ctx:
            svc.adapter(PROVIDER_YANDEX_MARKET).price_apply(
                sku="s1", amount=Decimal("100"), idempotency_key="k1"
            )
        self.assertEqual(ctx.exception.code, MARKETPLACE_CAPABILITY_UNSUPPORTED)

    def test_ozon_review_reply_absent(self):
        caps = svc = _svc()
        self.assertNotIn("REVIEW_REPLY", caps.capability_matrix()[PROVIDER_OZON])


class SelectiveExportTests(unittest.TestCase):
    def test_e2e_a_five_of_hundred(self):
        svc = _svc()
        catalog = _catalog(100)
        sku_ids = tuple(f"sku-{i}" for i in range(5))
        sel = new_selection(tenant_id="tenant-a", sku_ids=sku_ids)
        preview = svc.selection_preview(tenant_id="tenant-a", selection=sel, catalog=catalog)
        self.assertEqual(preview["selected_count"], 5)
        self.assertEqual({r["sku_id"] for r in preview["selected"]}, set(sku_ids))
        self.assertFalse(preview["external_mutation"])

    def test_e2e_b_no_implicit_full_export(self):
        svc = _svc()
        with self.assertRaises(MarketplaceError) as ctx:
            svc.selection_preview(tenant_id="tenant-a", selection=None, catalog=_catalog(10))
        self.assertEqual(ctx.exception.code, MARKETPLACE_SELECTION_REQUIRED)
        empty = new_selection(tenant_id="tenant-a")
        with self.assertRaises(MarketplaceError) as ctx2:
            svc.selection_preview(tenant_id="tenant-a", selection=empty, catalog=_catalog(10))
        self.assertEqual(ctx2.exception.code, MARKETPLACE_SELECTION_REQUIRED)


class CardLifecycleTests(unittest.TestCase):
    def test_e2e_cde_wb_ozon_yandex_cards(self):
        svc = _svc()
        item = _catalog(1)[0]
        for provider in (PROVIDER_WILDBERRIES, PROVIDER_OZON, PROVIDER_YANDEX_MARKET):
            acc = svc.register_account(tenant_id="tenant-a", provider=provider, credential_ref="secret:x")
            sel = new_selection(tenant_id="tenant-a", sku_ids=(item["sku_id"],))
            preview = svc.publication_plan(
                tenant_id="tenant-a",
                provider=provider,
                account_id=acc.account_id,
                selection=sel,
                catalog=[item],
                dry_run=True,
            )
            self.assertTrue(preview.dry_run)
            self.assertEqual(len(preview.creates), 1)
            plan = svc.publication_plan(
                tenant_id="tenant-a",
                provider=provider,
                account_id=acc.account_id,
                selection=sel,
                catalog=[item],
                dry_run=False,
            )
            out = svc.apply_publication(tenant_id="tenant-a", plan=plan, authorized=True)
            self.assertEqual(out["applied"], 1)
            self.assertFalse(out["live"])
            # content/media/seo handoffs
            self.assertEqual(svc.content_handoff(item=item, provider=provider)["delegate_to"], "content_intel")
            self.assertEqual(svc.media_handoff(item=item, provider=provider)["delegate_to"], "product_media")
            self.assertEqual(svc.seo_handoff(item=item, provider=provider)["delegate_to"], "seo_marketing")
            q = svc.card_quality_score(provider=provider, item=item)
            self.assertGreaterEqual(q["score"], 0.5)


class EconomicsTests(unittest.TestCase):
    def test_e2e_f_minimum_price_decimal(self):
        svc = _svc()
        _seed_commission(svc, PROVIDER_WILDBERRIES, "0.15")
        out = svc.minimum_price(
            purchase_cost=Decimal("1000"),
            provider=PROVIDER_WILDBERRIES,
            category="phones",
            logistics=Decimal("100"),
            acquiring_rate=Decimal("0.02"),
            policy=MarketplaceMinPricePolicy(policy_id="p1", required_margin_pct=Decimal("10")),
        )
        self.assertEqual(out["status"], "OK")
        # (1000+100+10) / (1-0.15-0.02-0.10) = 1110 / 0.73
        expected = (Decimal("1110") / Decimal("0.73")).quantize(Decimal("0.01"))
        self.assertEqual(Decimal(out["minimum_allowed"]), expected)

    def test_e2e_g_loss(self):
        svc = _svc()
        _seed_commission(svc, PROVIDER_WILDBERRIES, "0.15")
        econ = svc.profitability(
            sku_id="sku-1",
            provider=PROVIDER_WILDBERRIES,
            selling_price=Decimal("500"),
            purchase_cost=Decimal("1000"),
            category="phones",
            logistics=Decimal("100"),
        )
        self.assertEqual(econ["status"], PROFIT_LOSS)

    def test_unknown_cost(self):
        r = calculate_profitability(
            sku_id="s",
            provider=PROVIDER_OZON,
            selling_price=Decimal("100"),
            purchase_cost=None,
            commission=None,
            logistics=None,
        )
        self.assertEqual(r.status, PROFIT_UNKNOWN)


class AutoCorrectTests(unittest.TestCase):
    def test_e2e_h_auto_correct_supported(self):
        svc = _svc()
        _seed_commission(svc, PROVIDER_WILDBERRIES, "0.15")
        acc = svc.register_account(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, credential_ref="secret:wb")
        floor = Decimal(svc.minimum_price(
            purchase_cost=Decimal("1000"),
            provider=PROVIDER_WILDBERRIES,
            category="phones",
            logistics=Decimal("100"),
        )["minimum_allowed"])
        out = svc.loss_guard(
            tenant_id="tenant-a",
            account_id=acc.account_id,
            provider=PROVIDER_WILDBERRIES,
            sku_id="sku-1",
            selling_price=Decimal("500"),
            purchase_cost=Decimal("1000"),
            category="phones",
            logistics=Decimal("100"),
            mode=MODE_AUTO_CORRECT,
            authorized=True,
            proposed_correction=floor,
        )
        self.assertTrue(out["loss"])
        self.assertTrue(out["mutated"])
        ack = svc.acknowledge_price_reflection(causation_id=out["causation_id"])
        self.assertTrue(ack["terminated"])
        self.assertEqual(ack["code"], MARKETPLACE_SYNC_LOOP_TERMINATED)

    def test_e2e_i_auto_correct_unsupported_yandex(self):
        svc = _svc()
        _seed_commission(svc, PROVIDER_YANDEX_MARKET, "0.12")
        acc = svc.register_account(tenant_id="tenant-a", provider=PROVIDER_YANDEX_MARKET, credential_ref="secret:ym")
        out = svc.loss_guard(
            tenant_id="tenant-a",
            account_id=acc.account_id,
            provider=PROVIDER_YANDEX_MARKET,
            sku_id="sku-1",
            selling_price=Decimal("500"),
            purchase_cost=Decimal("1000"),
            category="phones",
            logistics=Decimal("100"),
            mode=MODE_AUTO_CORRECT,
            authorized=True,
        )
        self.assertTrue(out["loss"])
        self.assertFalse(out["mutated"])
        self.assertIn("alert_id", out)
        self.assertEqual(out["decision"]["code"], MARKETPLACE_AUTO_CORRECT_DENIED)


class PromoOwnershipTests(unittest.TestCase):
    def test_e2e_j_platform_discount_not_seller_loss(self):
        svc = _svc()
        _seed_commission(svc, PROVIDER_OZON, "0.15")
        promo = MarketplacePromotionObservation(
            promotion_id="pr1",
            provider=PROVIDER_OZON,
            sku_id="sku-1",
            ownership=PROMO_PLATFORM,
            displayed_price=MoneyAmount(Decimal("800")),
            seller_price=MoneyAmount(Decimal("2000")),
            platform_discount=MoneyAmount(Decimal("1200")),
            seller_discount=None,
        )
        analysis = svc.promotion_analysis(
            promo=promo,
            purchase_cost=Decimal("1000"),
            category="phones",
            logistics=Decimal("50"),
        )
        self.assertEqual(analysis["ownership"], PROMO_PLATFORM)
        self.assertEqual(analysis["risk"]["risk"], "SAFE")

    def test_e2e_k_seller_promo_risk(self):
        svc = _svc()
        _seed_commission(svc, PROVIDER_OZON, "0.15")
        promo = MarketplacePromotionObservation(
            promotion_id="pr2",
            provider=PROVIDER_OZON,
            sku_id="sku-1",
            ownership=PROMO_SELLER,
            displayed_price=MoneyAmount(Decimal("500")),
            seller_price=MoneyAmount(Decimal("500")),
            platform_discount=None,
            seller_discount=MoneyAmount(Decimal("500")),
        )
        analysis = svc.promotion_analysis(
            promo=promo,
            purchase_cost=Decimal("1000"),
            category="phones",
            logistics=Decimal("100"),
        )
        self.assertIn(analysis["risk"]["risk"], {"LOSS", "WARNING"})


class StockTests(unittest.TestCase):
    def test_e2e_l_buffer_and_match(self):
        svc = _svc()
        plan = svc.plan_stock_export(available=Decimal("10"), buffer=Decimal("2"))
        self.assertEqual(plan["channel_quantity"], "8")
        svc.apply_stock(
            provider=PROVIDER_WILDBERRIES,
            sku="sku-1",
            quantity=Decimal("8"),
            idempotency_key="st1",
        )
        rec = svc.reconcile_channel_stock(provider=PROVIDER_WILDBERRIES, sku="sku-1", expected=Decimal("8"))
        self.assertEqual(rec["status"], STOCK_MATCHED)

    def test_e2e_m_drift(self):
        svc = _svc()
        svc.apply_stock(
            provider=PROVIDER_OZON,
            sku="sku-2",
            quantity=Decimal("3"),
            idempotency_key="st2",
        )
        rec = svc.reconcile_channel_stock(provider=PROVIDER_OZON, sku="sku-2", expected=Decimal("8"))
        self.assertEqual(rec["status"], STOCK_DRIFT)


class OrderTests(unittest.TestCase):
    def test_e2e_n_order_idempotency(self):
        svc = _svc()
        items = [{"sku": "sku-1", "quantity": "1", "unit_price": "100"}]
        o1 = svc.ingest_marketplace_order(
            tenant_id="tenant-a",
            provider=PROVIDER_WILDBERRIES,
            external_order_id="wb-ord-1",
            items=items,
        )
        o2 = svc.ingest_marketplace_order(
            tenant_id="tenant-a",
            provider=PROVIDER_WILDBERRIES,
            external_order_id="wb-ord-1",
            items=items,
        )
        self.assertEqual(o1["order_id"], o2["order_id"])
        self.assertTrue(o2["idempotent"])


class ReviewCompetitorTests(unittest.TestCase):
    def test_e2e_o_reviews(self):
        svc = _svc()
        svc.adapter(PROVIDER_WILDBERRIES).seed_review(
            {"external_id": "r1", "sku_id": "sku-1", "rating": 2, "text": "Плохая доставка и брак"}
        )
        reviews = svc.sync_reviews(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, account_id="acc")
        self.assertEqual(len(reviews), 1)
        self.assertIn("delivery", reviews[0].topics)
        draft = svc.draft_reply(reviews[0])
        self.assertTrue(draft["requires_governed_write"])
        self.assertFalse(draft["external_applied"])

    def test_e2e_p_competitive_floor_guard(self):
        svc = _svc()
        out = svc.competitor_scan(
            sku_id="sku-1",
            provider=PROVIDER_WILDBERRIES,
            our_price=Decimal("2000"),
            our_ean="4601234567890",
            candidates=[
                {"ean": "4601234567890", "price": "1500", "seller": "comp"},
                {"title": "something similar phone", "price": "100", "model": "X"},  # ambiguous ignored
            ],
            minimum_allowed=Decimal("1800"),
        )
        self.assertEqual(out["matched"], 1)
        self.assertTrue(out["recommendation"]["blocked_undercut"])
        self.assertEqual(out["recommendation"]["proposed"], "1800")


class CommissionChangeTests(unittest.TestCase):
    def test_e2e_q_commission_change(self):
        svc = _svc()
        _seed_commission(svc, PROVIDER_OZON, "0.10")
        before = svc.profitability(
            sku_id="sku-1",
            provider=PROVIDER_OZON,
            selling_price=Decimal("1500"),
            purchase_cost=Decimal("1000"),
            category="phones",
            logistics=Decimal("50"),
        )
        _seed_commission(svc, PROVIDER_OZON, "0.40")
        after = svc.profitability(
            sku_id="sku-1",
            provider=PROVIDER_OZON,
            selling_price=Decimal("1500"),
            purchase_cost=Decimal("1000"),
            category="phones",
            logistics=Decimal("50"),
        )
        self.assertNotEqual(before["status"], after["status"])


class LoopAndOverrideTests(unittest.TestCase):
    def test_e2e_r_s_loop_and_override(self):
        svc = _svc()
        _seed_commission(svc, PROVIDER_WILDBERRIES, "0.15")
        acc = svc.register_account(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, credential_ref="secret:wb")
        floor = Decimal(svc.minimum_price(
            purchase_cost=Decimal("1000"),
            provider=PROVIDER_WILDBERRIES,
            category="phones",
            logistics=Decimal("100"),
        )["minimum_allowed"])
        out = svc.loss_guard(
            tenant_id="tenant-a",
            account_id=acc.account_id,
            provider=PROVIDER_WILDBERRIES,
            sku_id="sku-loop",
            selling_price=Decimal("500"),
            purchase_cost=Decimal("1000"),
            category="phones",
            logistics=Decimal("100"),
            mode=MODE_AUTO_CORRECT,
            authorized=True,
            proposed_correction=floor,
        )
        ack = svc.acknowledge_price_reflection(causation_id=out["causation_id"])
        self.assertTrue(ack["terminated"])
        o1 = svc.note_external_price_override(
            tenant_id="tenant-a", account_id=acc.account_id, provider=PROVIDER_WILDBERRIES, sku_id="sku-loop", amount=Decimal("400")
        )
        o2 = svc.note_external_price_override(
            tenant_id="tenant-a", account_id=acc.account_id, provider=PROVIDER_WILDBERRIES, sku_id="sku-loop", amount=Decimal("390")
        )
        self.assertTrue(o2["stop"])
        self.assertIn("alert_id", o2)


class ProviderIsolationTests(unittest.TestCase):
    def test_e2e_t_wb_down_ozon_ok(self):
        svc = _svc()
        svc.isolate_provider(PROVIDER_WILDBERRIES, unavailable=True)
        self.assertFalse(svc.health(PROVIDER_WILDBERRIES)["reachable"])
        self.assertTrue(svc.health(PROVIDER_OZON)["reachable"])
        self.assertTrue(svc.health(PROVIDER_YANDEX_MARKET)["reachable"])
        item = _catalog(1)[0]
        acc = svc.register_account(tenant_id="tenant-a", provider=PROVIDER_OZON, credential_ref="secret:oz")
        sel = new_selection(tenant_id="tenant-a", sku_ids=(item["sku_id"],))
        plan = svc.publication_plan(
            tenant_id="tenant-a",
            provider=PROVIDER_OZON,
            account_id=acc.account_id,
            selection=sel,
            catalog=[item],
            dry_run=False,
        )
        out = svc.apply_publication(tenant_id="tenant-a", plan=plan)
        self.assertEqual(out["applied"], 1)


class BatchJobTests(unittest.TestCase):
    def test_e2e_u_large_batch_checkpoint(self):
        svc = _svc()
        items = [{"product_id": f"p-{i}", "sku_id": f"sku-{i}"} for i in range(250)]
        r1 = svc.start_batch_job(
            tenant_id="tenant-a",
            job_type="CARD_CREATE",
            provider=PROVIDER_OZON,
            items=items,
            partition_size=100,
            resume_from=0,
        )
        self.assertEqual(r1["status"], "partial")
        self.assertEqual(r1["checkpoint"], 100)
        r2 = svc.start_batch_job(
            tenant_id="tenant-a",
            job_type="CARD_CREATE",
            provider=PROVIDER_OZON,
            items=items,
            partition_size=100,
            resume_from=r1["checkpoint"],
            job_id=r1["job_id"],
        )
        self.assertEqual(r2["checkpoint"], 200)
        cancelled = svc.cancel_job(tenant_id="tenant-a", job_id=r1["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaises(MarketplaceError) as ctx:
            svc.start_batch_job(
                tenant_id="tenant-a",
                job_type="CARD_CREATE",
                provider=PROVIDER_OZON,
                items=items,
                partition_size=100,
                resume_from=200,
                job_id=r1["job_id"],
            )
        self.assertEqual(ctx.exception.code, "MARKETPLACE_CANCELLED")


class TenantSecurityTests(unittest.TestCase):
    def test_e2e_v_cross_tenant(self):
        svc = _svc()
        acc = svc.register_account(tenant_id="tenant-a", provider=PROVIDER_OZON, credential_ref="secret:a")
        with self.assertRaises(MarketplaceError) as ctx:
            svc.get_account(tenant_id="tenant-b", account_id=acc.account_id)
        self.assertEqual(ctx.exception.code, MARKETPLACE_CROSS_TENANT)

    def test_secret_ref_only(self):
        svc = _svc()
        acc = svc.register_account(tenant_id="tenant-a", provider=PROVIDER_OZON, credential_ref="rawtoken")
        self.assertTrue(acc.credential_ref.startswith("secret:"))
        self.assertNotIn("Authorization", acc.credential_ref)


class MappingAmbiguityTests(unittest.TestCase):
    def test_category_unmapped(self):
        svc = _svc()
        m = svc.map_category(provider=PROVIDER_WILDBERRIES, canonical_category_id="unknown-cat")
        self.assertEqual(m["status"], "UNMAPPED")

    def test_missing_required_attr(self):
        svc = _svc()
        v = svc.validate_card(
            provider=PROVIDER_YANDEX_MARKET,
            item={"sku_id": "s", "title": "t", "category_id": "phones", "attributes": {"brand": "Acme"}},
        )
        self.assertFalse(v["ok"])
        self.assertTrue(any("attr_missing" in e for e in v["errors"]))


class MonitorDefaultTests(unittest.TestCase):
    def test_default_mode_not_auto(self):
        svc = _svc()
        self.assertEqual(svc._correction_mode, MODE_MONITOR_ONLY)


class DashboardAnalyticsTests(unittest.TestCase):
    def test_analytics_no_invention(self):
        svc = _svc()
        summary = svc.analytics_summary(
            tenant_id="tenant-a",
            provider=PROVIDER_OZON,
            sales=[{"revenue": "100", "units": "2"}, {"revenue": "50", "units": "1"}],
        )
        self.assertEqual(summary["revenue"], "150")
        self.assertFalse(summary["invented"])
        dash = svc.dashboard(tenant_id="tenant-a")
        self.assertIn("provider_health", dash)


if __name__ == "__main__":
    unittest.main()
