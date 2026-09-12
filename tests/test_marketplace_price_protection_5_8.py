"""Block 5.8 — Marketplace Price Protection targeted tests.

Deterministic unit/fixture tests only. No real marketplace API calls
anywhere in this file (WB/Ozon/Yandex Market FIXTURE adapters only, same
governed path as Block 5.7A).
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from integrations.activation.models import ENV_FIXTURE
from integrations.activation.service import IntegrationActivationService
from integrations.ozon.catalog import OzonCatalogStore
from integrations.wildberries.catalog import WildberriesCatalogStore
from integrations.yandex_market.catalog import YandexMarketCatalogStore

from marketplace.errors import MARKETPLACE_APPROVAL_REQUIRED, MarketplaceError
from marketplace.models import MoneyAmount, PROVIDER_OZON, PROVIDER_WILDBERRIES, PROVIDER_YANDEX_MARKET
from marketplace.platform import MarketplacePlatform
from marketplace.price_protection import (
    PRICE_DECISION_ALLOW,
    PRICE_DECISION_BLOCK,
    PRICE_DECISION_REQUIRE_APPROVAL,
    PROFITABILITY_LOSS_MAKING,
    REASON_BELOW_MINIMUM_ALLOWED_PRICE,
    REASON_CRITICAL_PRICE_CHANGE,
    REASON_CURRENCY_MISMATCH,
    REASON_INVALID_ECONOMIC_INPUT,
    REASON_LOSS_MAKING,
    REASON_PRICE_DROP_LIMIT_EXCEEDED,
    REASON_PRICE_INCREASE_LIMIT_EXCEEDED,
    PriceProtectionContext,
    PriceProtectionPolicy,
    PriceProtectionPolicyStore,
    calculate_minimum_allowed_price,
    evaluate_price_decision,
)


def _isolated_service() -> IntegrationActivationService:
    """Same isolation fix already established in
    ``tests/test_marketplace_platform_5_7a.py`` -- every test in *this*
    file gets its own catalog stores rather than the shared
    ``GLOBAL_*_CATALOG`` singletons, so price writes here cannot pollute
    unrelated pre-existing tests."""
    svc = IntegrationActivationService()
    svc._wb_fixture._store = WildberriesCatalogStore()  # noqa: SLF001
    svc._ozon_fixture._store = OzonCatalogStore()  # noqa: SLF001
    svc._ym_fixture._store = YandexMarketCatalogStore()  # noqa: SLF001
    return svc


def _activated_connection(svc: IntegrationActivationService, *, tenant_id: str, provider_id: str, environment: str = ENV_FIXTURE):
    conn = svc.configure_connection(
        tenant_id=tenant_id, provider_id=provider_id, credential_ref=f"secret:{provider_id}-demo", environment=environment,
    )
    svc.verify_connection(tenant_id=tenant_id, connection_id=conn.connection_id)
    svc.activate_connection(tenant_id=tenant_id, connection_id=conn.connection_id)
    return conn


def _ctx(**overrides) -> PriceProtectionContext:
    base = dict(tenant_id="tenant-a", product_id="p1", sku="SKU-1", provider=PROVIDER_WILDBERRIES, currency="RUB", purchase_cost=Decimal("1000"))
    base.update(overrides)
    return PriceProtectionContext(**base)


# =====================================================================
# 1. Minimum allowed price — fixed + proportional cost math (cases 1-8)
# =====================================================================
class MinimumAllowedPriceMathTests(unittest.TestCase):
    def test_fixed_cost_only_minimum_price(self):
        context = _ctx(additional_unit_cost=Decimal("50"), logistics_cost=Decimal("100"), packaging_cost=Decimal("20"), other_costs=Decimal("30"))
        amount, status, problems = calculate_minimum_allowed_price(context)
        self.assertEqual(status, "OK")
        self.assertEqual(problems, ())
        self.assertEqual(amount.amount, Decimal("1200.00"))

    def test_percentage_commission_included_correctly(self):
        context = _ctx(commission_rate=Decimal("0.10"))
        amount, status, _ = calculate_minimum_allowed_price(context)
        self.assertEqual(status, "OK")
        self.assertEqual(amount.amount, Decimal("1111.11"))

    def test_acquiring_percentage_included_correctly(self):
        context = _ctx(acquiring_rate=Decimal("0.10"))
        amount, _, _ = calculate_minimum_allowed_price(context)
        self.assertEqual(amount.amount, Decimal("1111.11"))

    def test_advertising_percentage_included_correctly(self):
        context = _ctx(advertising_rate=Decimal("0.10"))
        amount, _, _ = calculate_minimum_allowed_price(context)
        self.assertEqual(amount.amount, Decimal("1111.11"))

    def test_combined_proportional_costs_solved_mathematically_not_approximated(self):
        """purchase=1000, rate stack=0.20 (commission 0.10 + acquiring 0.05
        + advertising 0.03 + tax 0.02). Correct algebra: 1000 / 0.80 =
        1250.00. A naive (wrong) approximation of cost*(1+rate) would give
        1200.00 instead -- this proves the real equation is solved."""
        context = _ctx(commission_rate=Decimal("0.10"), acquiring_rate=Decimal("0.05"), advertising_rate=Decimal("0.03"), tax_rate=Decimal("0.02"))
        amount, _, _ = calculate_minimum_allowed_price(context)
        self.assertEqual(amount.amount, Decimal("1250.00"))
        self.assertNotEqual(amount.amount, Decimal("1200.00"))

    def test_absolute_minimum_profit_floor(self):
        context = _ctx(minimum_profit_amount=Decimal("200"))
        amount, _, _ = calculate_minimum_allowed_price(context)
        self.assertEqual(amount.amount, Decimal("1200.00"))

    def test_minimum_margin_floor(self):
        context = _ctx(minimum_margin_rate=Decimal("0.20"))
        amount, _, _ = calculate_minimum_allowed_price(context)
        self.assertEqual(amount.amount, Decimal("1250.00"))

    def test_stricter_of_profit_or_margin_wins_margin_side(self):
        context = _ctx(minimum_profit_amount=Decimal("200"), minimum_margin_rate=Decimal("0.20"))
        amount, _, _ = calculate_minimum_allowed_price(context)
        self.assertEqual(amount.amount, Decimal("1250.00"))  # margin (1250) > profit (1200)

    def test_stricter_of_profit_or_margin_wins_profit_side(self):
        context = _ctx(minimum_profit_amount=Decimal("400"), minimum_margin_rate=Decimal("0.20"))
        amount, _, _ = calculate_minimum_allowed_price(context)
        self.assertEqual(amount.amount, Decimal("1400.00"))  # profit (1400) > margin (1250)

    def test_no_profit_policy_configured_defaults_to_breakeven_not_zero(self):
        context = _ctx()
        amount, _, _ = calculate_minimum_allowed_price(context)
        self.assertEqual(amount.amount, Decimal("1000.00"))


# =====================================================================
# 2. Fail-closed / invalid economics (cases 18, 19, 20, 21)
# =====================================================================
class FailClosedTests(unittest.TestCase):
    def test_missing_purchase_cost_is_invalid_not_defaulted_to_zero(self):
        context = _ctx(purchase_cost=None)
        amount, status, problems = calculate_minimum_allowed_price(context)
        self.assertIsNone(amount)
        self.assertEqual(status, "INVALID_ECONOMIC_INPUT")
        self.assertIn("purchase_cost_missing", problems)

    def test_invalid_percentage_out_of_domain_is_rejected(self):
        context = _ctx(commission_rate=Decimal("1.5"))
        amount, status, problems = calculate_minimum_allowed_price(context)
        self.assertIsNone(amount)
        self.assertEqual(status, "INVALID_ECONOMIC_INPUT")
        self.assertIn("commission_rate_out_of_domain", problems)

    def test_impossible_proportional_economics_rate_sum_exceeds_price(self):
        context = _ctx(commission_rate=Decimal("0.5"), acquiring_rate=Decimal("0.3"), advertising_rate=Decimal("0.25"))
        amount, status, problems = calculate_minimum_allowed_price(context)
        self.assertIsNone(amount)
        self.assertEqual(status, "INVALID_ECONOMIC_INPUT")

    def test_impossible_margin_plus_proportional_economics(self):
        context = _ctx(commission_rate=Decimal("0.30"), acquiring_rate=Decimal("0.30"), minimum_margin_rate=Decimal("0.50"))
        amount, status, problems = calculate_minimum_allowed_price(context)
        self.assertIsNone(amount)
        self.assertEqual(status, "INVALID_ECONOMIC_INPUT")
        self.assertIn("margin_plus_proportional_costs_exceed_price", problems)

    def test_negative_fixed_cost_is_rejected(self):
        context = _ctx(logistics_cost=Decimal("-5"))
        amount, status, problems = calculate_minimum_allowed_price(context)
        self.assertIsNone(amount)
        self.assertEqual(status, "INVALID_ECONOMIC_INPUT")
        self.assertIn("logistics_cost_negative", problems)

    def test_currency_mismatch_blocks_via_decision_engine(self):
        context = _ctx()
        policy = PriceProtectionPolicy()
        decision = evaluate_price_decision(
            context=context, policy=policy,
            proposed_price=MoneyAmount(Decimal("2000"), "USD"),  # context currency is RUB
        )
        self.assertEqual(decision.outcome, PRICE_DECISION_BLOCK)
        self.assertIn(REASON_CURRENCY_MISMATCH, decision.reason_codes)

    def test_invalid_economic_input_surfaces_as_block_decision(self):
        context = _ctx(purchase_cost=None)
        policy = PriceProtectionPolicy()
        decision = evaluate_price_decision(context=context, policy=policy, proposed_price=MoneyAmount(Decimal("2000"), "RUB"))
        self.assertEqual(decision.outcome, PRICE_DECISION_BLOCK)
        self.assertIn(REASON_INVALID_ECONOMIC_INPUT, decision.reason_codes)


# =====================================================================
# 3. Decision engine — hard floor vs policy-required approval (cases 9-17)
# =====================================================================
class DecisionEngineTests(unittest.TestCase):
    def setUp(self):
        self.context = _ctx(commission_rate=Decimal("0.10"), acquiring_rate=Decimal("0.05"), advertising_rate=Decimal("0.03"), tax_rate=Decimal("0.02"))
        # minimum_allowed_price == 1250.00 for this context (see math tests above)
        self.policy = PriceProtectionPolicy()

    def test_safe_price_allows(self):
        decision = evaluate_price_decision(context=self.context, policy=self.policy, proposed_price=MoneyAmount(Decimal("2000"), "RUB"))
        self.assertEqual(decision.outcome, PRICE_DECISION_ALLOW)
        self.assertEqual(decision.reason_codes, ())

    def test_price_exactly_at_hard_floor_allows(self):
        decision = evaluate_price_decision(context=self.context, policy=self.policy, proposed_price=MoneyAmount(Decimal("1250.00"), "RUB"))
        self.assertEqual(decision.outcome, PRICE_DECISION_ALLOW)

    def test_price_one_unit_below_floor_blocks(self):
        decision = evaluate_price_decision(context=self.context, policy=self.policy, proposed_price=MoneyAmount(Decimal("1249.99"), "RUB"))
        self.assertEqual(decision.outcome, PRICE_DECISION_BLOCK)
        self.assertIn(REASON_BELOW_MINIMUM_ALLOWED_PRICE, decision.reason_codes)

    def test_loss_making_price_blocks_with_loss_making_reason_first(self):
        decision = evaluate_price_decision(context=self.context, policy=self.policy, proposed_price=MoneyAmount(Decimal("500"), "RUB"))
        self.assertEqual(decision.outcome, PRICE_DECISION_BLOCK)
        self.assertEqual(decision.reason_codes[0], REASON_LOSS_MAKING)
        self.assertIn(REASON_BELOW_MINIMUM_ALLOWED_PRICE, decision.reason_codes)
        self.assertEqual(decision.profitability_status, PROFITABILITY_LOSS_MAKING)

    def test_critical_but_profitable_decrease_requires_approval(self):
        policy = PriceProtectionPolicy(critical_change_percent=Decimal("5"), require_approval_for_critical_change=True)
        decision = evaluate_price_decision(
            context=self.context, policy=policy,
            proposed_price=MoneyAmount(Decimal("1800"), "RUB"), current_price=MoneyAmount(Decimal("2000"), "RUB"),
        )
        self.assertEqual(decision.outcome, PRICE_DECISION_REQUIRE_APPROVAL)
        self.assertIn(REASON_CRITICAL_PRICE_CHANGE, decision.reason_codes)

    def test_approval_cannot_bypass_hard_economic_floor(self):
        """Even a policy that never requires approval-review cannot make a
        below-floor price acceptable -- the hard floor is unconditional."""
        policy = PriceProtectionPolicy(critical_change_percent=None, require_approval_for_critical_change=False)
        decision = evaluate_price_decision(context=self.context, policy=policy, proposed_price=MoneyAmount(Decimal("1249.99"), "RUB"))
        self.assertEqual(decision.outcome, PRICE_DECISION_BLOCK)

    def test_price_drop_policy_limit_exceeded_requires_approval_not_block(self):
        policy = PriceProtectionPolicy(maximum_price_drop_percent=Decimal("10"), critical_change_percent=None, require_approval_for_critical_change=False)
        decision = evaluate_price_decision(
            context=self.context, policy=policy,
            proposed_price=MoneyAmount(Decimal("1700"), "RUB"), current_price=MoneyAmount(Decimal("2000"), "RUB"),  # 15% drop, still above the 1250 floor
        )
        self.assertEqual(decision.outcome, PRICE_DECISION_REQUIRE_APPROVAL)
        self.assertIn(REASON_PRICE_DROP_LIMIT_EXCEEDED, decision.reason_codes)

    def test_price_increase_policy_limit_exceeded_requires_approval(self):
        policy = PriceProtectionPolicy(maximum_price_increase_percent=Decimal("10"), critical_change_percent=None, require_approval_for_critical_change=False)
        decision = evaluate_price_decision(
            context=self.context, policy=policy,
            proposed_price=MoneyAmount(Decimal("1800"), "RUB"), current_price=MoneyAmount(Decimal("1500"), "RUB"),  # 20% increase
        )
        self.assertEqual(decision.outcome, PRICE_DECISION_REQUIRE_APPROVAL)
        self.assertIn(REASON_PRICE_INCREASE_LIMIT_EXCEEDED, decision.reason_codes)

    def test_price_within_drop_limit_allows(self):
        policy = PriceProtectionPolicy(maximum_price_drop_percent=Decimal("10"), critical_change_percent=None, require_approval_for_critical_change=False)
        decision = evaluate_price_decision(
            context=self.context, policy=policy,
            proposed_price=MoneyAmount(Decimal("1900"), "RUB"), current_price=MoneyAmount(Decimal("2000"), "RUB"),  # 5% drop, within limit
        )
        self.assertEqual(decision.outcome, PRICE_DECISION_ALLOW)


# =====================================================================
# 4. Provider-specific cost profiles produce provider-specific floors (case 22)
# =====================================================================
class ProviderProfileTests(unittest.TestCase):
    def test_same_sku_different_provider_profiles_yield_different_floors(self):
        wb = _ctx(provider=PROVIDER_WILDBERRIES, commission_rate=Decimal("0.15"))
        ozon = _ctx(provider=PROVIDER_OZON, commission_rate=Decimal("0.20"))
        ym = _ctx(provider=PROVIDER_YANDEX_MARKET, commission_rate=Decimal("0.10"))
        wb_min, _, _ = calculate_minimum_allowed_price(wb)
        ozon_min, _, _ = calculate_minimum_allowed_price(ozon)
        ym_min, _, _ = calculate_minimum_allowed_price(ym)
        self.assertNotEqual(wb_min.amount, ozon_min.amount)
        self.assertNotEqual(ozon_min.amount, ym_min.amount)
        self.assertNotEqual(wb_min.amount, ym_min.amount)
        # higher commission rate -> strictly higher required floor for identical fixed costs
        self.assertGreater(ozon_min.amount, ym_min.amount)


# =====================================================================
# 5. Policy precedence / tenant isolation (cases 23-25)
# =====================================================================
class PolicyPrecedenceTests(unittest.TestCase):
    def test_tenant_policy_isolation(self):
        store = PriceProtectionPolicyStore()
        store.set_tenant_default(tenant_id="tenant-a", policy=PriceProtectionPolicy(policy_id="tenant-a-default", maximum_price_drop_percent=Decimal("5")))
        resolved_a = store.resolve(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, sku="SKU-1")
        resolved_b = store.resolve(tenant_id="tenant-b", provider=PROVIDER_WILDBERRIES, sku="SKU-1")
        self.assertEqual(resolved_a.policy_id, "tenant-a-default")
        self.assertEqual(resolved_b.policy_id, "system_default")
        self.assertIsNone(resolved_b.maximum_price_drop_percent)

    def test_sku_override_beats_provider_policy_beats_tenant_default(self):
        store = PriceProtectionPolicyStore()
        store.set_tenant_default(tenant_id="tenant-a", policy=PriceProtectionPolicy(policy_id="tenant-default"))
        store.set_provider_policy(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, policy=PriceProtectionPolicy(policy_id="wb-provider-policy"))
        store.set_sku_override(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, sku="SKU-1", policy=PriceProtectionPolicy(policy_id="sku-1-override"))

        self.assertEqual(store.resolve(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, sku="SKU-1").policy_id, "sku-1-override")
        self.assertEqual(store.resolve(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, sku="SKU-OTHER").policy_id, "wb-provider-policy")
        self.assertEqual(store.resolve(tenant_id="tenant-a", provider=PROVIDER_OZON, sku="SKU-1").policy_id, "tenant-default")


# =====================================================================
# 6. Batch — per-item results, no cross-contamination (case 28)
# =====================================================================
class BatchTests(unittest.TestCase):
    def test_batch_mixed_allow_require_approval_block_remains_per_item(self):
        svc = _isolated_service()
        platform = MarketplacePlatform(activation=svc, provider=PROVIDER_WILDBERRIES)
        safe_context = _ctx(sku="SKU-SAFE")
        critical_context = _ctx(sku="SKU-CRITICAL")
        loss_context = _ctx(sku="SKU-LOSS")
        items = [
            {"external_sku": "SKU-SAFE", "context": safe_context, "proposed_price": Decimal("2000")},
            {
                "external_sku": "SKU-CRITICAL", "context": critical_context, "proposed_price": Decimal("1800"),
                "current_price": Decimal("2000"),
            },
            {"external_sku": "SKU-LOSS", "context": loss_context, "proposed_price": Decimal("100")},
        ]
        platform.price_protection_policies.set_sku_override(
            tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, sku="SKU-CRITICAL",
            policy=PriceProtectionPolicy(critical_change_percent=Decimal("5"), require_approval_for_critical_change=True),
        )
        results = platform.evaluate_price_protection_batch(tenant_id="tenant-a", items=items)
        by_sku = {r["external_sku"]: r["decision"] for r in results}
        self.assertEqual(len(results), 3)
        self.assertEqual(by_sku["SKU-SAFE"].outcome, PRICE_DECISION_ALLOW)
        self.assertEqual(by_sku["SKU-CRITICAL"].outcome, PRICE_DECISION_REQUIRE_APPROVAL)
        self.assertEqual(by_sku["SKU-LOSS"].outcome, PRICE_DECISION_BLOCK)


# =====================================================================
# 7. End-to-end integration with the governed 5.7A write path
# =====================================================================
class GovernedWriteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.svc = _isolated_service()
        self.conn = _activated_connection(self.svc, tenant_id="tenant-a", provider_id="wildberries")
        self.platform = MarketplacePlatform(activation=self.svc, provider=PROVIDER_WILDBERRIES)

    def test_allow_decision_proceeds_through_existing_governed_write(self):
        context = _ctx(sku="WB-SKU-100")
        out = self.platform.write_price(
            tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("2000"),
            idempotency_key="pp-allow-1", approved_write=True, connection_id=self.conn.connection_id,
            protection=context,
        )
        self.assertEqual(out["status"], "WRITE_ACCEPTED")
        self.assertEqual(out["price_protection_decision"].outcome, PRICE_DECISION_ALLOW)

    def test_hard_block_prevents_write_even_when_approved(self):
        context = _ctx(sku="WB-SKU-100", commission_rate=Decimal("0.10"), acquiring_rate=Decimal("0.05"), advertising_rate=Decimal("0.03"), tax_rate=Decimal("0.02"))
        with self.assertRaises(MarketplaceError) as ctx:
            self.platform.write_price(
                tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("1249.99"),
                idempotency_key="pp-block-1", approved_write=True, connection_id=self.conn.connection_id,
                protection=context,
            )
        # with no profit/margin policy configured, the floor is exact
        # break-even, so anything below it is (correctly) LOSS_MAKING --
        # the more specific reason always leads.
        self.assertEqual(ctx.exception.code, REASON_LOSS_MAKING)
        self.assertIn(REASON_BELOW_MINIMUM_ALLOWED_PRICE, ctx.exception.decision.reason_codes)
        self.assertEqual(ctx.exception.decision.outcome, PRICE_DECISION_BLOCK)
        # never reached the provider -- price is unchanged.
        current = self.platform.read_price(tenant_id="tenant-a", external_sku="WB-SKU-100", connection_id=self.conn.connection_id)
        self.assertNotEqual(current.amount.amount, Decimal("1249.99"))

    def test_require_approval_without_approved_write_is_denied_and_does_not_mutate(self):
        context = _ctx(sku="WB-SKU-100")
        policy = PriceProtectionPolicy(critical_change_percent=Decimal("5"), require_approval_for_critical_change=True)
        self.platform.price_protection_policies.set_sku_override(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, sku="WB-SKU-100", policy=policy)

        before = self.platform.read_price(tenant_id="tenant-a", external_sku="WB-SKU-100", connection_id=self.conn.connection_id)
        with self.assertRaises(MarketplaceError) as ctx:
            self.platform.write_price(
                tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("1800"),
                idempotency_key="pp-req-1", approved_write=False, connection_id=self.conn.connection_id,
                protection=context, current_price=Decimal("2000"),
            )
        self.assertEqual(ctx.exception.code, MARKETPLACE_APPROVAL_REQUIRED)
        self.assertEqual(ctx.exception.decision.outcome, PRICE_DECISION_REQUIRE_APPROVAL)
        after = self.platform.read_price(tenant_id="tenant-a", external_sku="WB-SKU-100", connection_id=self.conn.connection_id)
        self.assertEqual(before.amount.amount, after.amount.amount)

    def test_require_approval_with_approved_write_proceeds(self):
        context = _ctx(sku="WB-SKU-100")
        policy = PriceProtectionPolicy(critical_change_percent=Decimal("5"), require_approval_for_critical_change=True)
        self.platform.price_protection_policies.set_sku_override(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, sku="WB-SKU-100", policy=policy)

        out = self.platform.write_price(
            tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("1800"),
            idempotency_key="pp-req-2", approved_write=True, connection_id=self.conn.connection_id,
            protection=context, current_price=Decimal("2000"),
        )
        self.assertEqual(out["status"], "WRITE_ACCEPTED")
        self.assertEqual(out["price_protection_decision"].outcome, PRICE_DECISION_REQUIRE_APPROVAL)

    def test_idempotent_replay_with_protection_does_not_duplicate_external_action(self):
        context = _ctx(sku="WB-SKU-100")
        first = self.platform.write_price(
            tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("2000"),
            idempotency_key="pp-idem-1", approved_write=True, connection_id=self.conn.connection_id,
            protection=context,
        )
        self.assertFalse(first.get("idempotent"))
        replay = self.platform.write_price(
            tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("2000"),
            idempotency_key="pp-idem-1", approved_write=True, connection_id=self.conn.connection_id,
            protection=context,
        )
        self.assertTrue(replay.get("idempotent"))

    def test_approval_does_not_leak_across_different_prices_or_skus(self):
        """Approving one specific (tenant, provider, sku, price) write must
        never silently authorize a different price or a different,
        separately-unsafe SKU -- each ``write_price`` call independently
        re-evaluates Price Protection; there is no cached/reusable
        approval state."""
        context = _ctx(sku="WB-SKU-100")
        policy = PriceProtectionPolicy(critical_change_percent=Decimal("5"), require_approval_for_critical_change=True)
        self.platform.price_protection_policies.set_sku_override(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, sku="WB-SKU-100", policy=policy)

        # Approval for price=1800 succeeds.
        self.platform.write_price(
            tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("1800"),
            idempotency_key="bind-price-1800", approved_write=True, connection_id=self.conn.connection_id,
            protection=context, current_price=Decimal("2000"),
        )
        # A different, unrelated price change on the SAME sku, in the SAME
        # test process, still independently requires its own approval --
        # calling it without approved_write must still be denied.
        with self.assertRaises(MarketplaceError) as ctx:
            self.platform.write_price(
                tenant_id="tenant-a", external_sku="WB-SKU-100", amount=Decimal("1700"),
                idempotency_key="bind-price-1700", approved_write=False, connection_id=self.conn.connection_id,
                protection=context, current_price=Decimal("2000"),
            )
        self.assertEqual(ctx.exception.code, MARKETPLACE_APPROVAL_REQUIRED)

        # A hard-unsafe price on a *different* SKU is still BLOCKed even
        # though this same test just successfully performed an approved
        # write moments ago -- approval never transfers across SKUs.
        loss_context = _ctx(sku="WB-SKU-200")
        with self.assertRaises(MarketplaceError) as ctx2:
            self.platform.write_price(
                tenant_id="tenant-a", external_sku="WB-SKU-200", amount=Decimal("50"),
                idempotency_key="bind-sku-200", approved_write=True, connection_id=self.conn.connection_id,
                protection=loss_context,
            )
        self.assertEqual(ctx2.exception.decision.outcome, PRICE_DECISION_BLOCK)

    def test_tenant_and_provider_isolation_of_price_protection_policies(self):
        wb_platform = self.platform
        ozon_conn = _activated_connection(self.svc, tenant_id="tenant-a", provider_id="ozon")
        ozon_platform = MarketplacePlatform(activation=self.svc, provider=PROVIDER_OZON)

        wb_policy = PriceProtectionPolicy(policy_id="wb-only", maximum_price_drop_percent=Decimal("5"))
        wb_platform.price_protection_policies.set_provider_policy(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, policy=wb_policy)

        resolved_wb = wb_platform.price_protection_policies.resolve(tenant_id="tenant-a", provider=PROVIDER_WILDBERRIES, sku="WB-SKU-100")
        resolved_ozon = ozon_platform.price_protection_policies.resolve(tenant_id="tenant-a", provider=PROVIDER_OZON, sku="OZ-SKU-100")
        self.assertEqual(resolved_wb.policy_id, "wb-only")
        self.assertEqual(resolved_ozon.policy_id, "system_default")
        del ozon_conn  # connection only needed to activate the provider for this isolation check


if __name__ == "__main__":
    unittest.main()
