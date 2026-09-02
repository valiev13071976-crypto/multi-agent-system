"""Analytics & Management Dashboard — closure tests."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal


def _range_query(**extra):
    today = datetime.now(timezone.utc)
    start = (today - timedelta(days=30)).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    base = {"start": start, "end": end}
    base.update(extra)
    return base

from fastapi.testclient import TestClient

from analytics_dashboard.config import (
    analytics_dashboard_engineering_ready,
    analytics_dashboard_live_active,
    analytics_dashboard_live_verified,
)
from analytics_dashboard.errors import AnalyticsError, LIVE_FALLBACK_FORBIDDEN, UNSUPPORTED_METRIC
from analytics_dashboard.metrics import METRIC_REGISTRY, get_metric
from analytics_dashboard.models import STATUS_NO_DATA, STATUS_OK, STATUS_PARTIAL, STATUS_UNAVAILABLE
from analytics_dashboard.models import AnalyticsQuery
from analytics_dashboard.router import configure_analytics_dashboard_router
from analytics_dashboard.runtime import build_analytics_dashboard_runtime
from analytics_dashboard.service import AnalyticsDashboardService
from analytics_dashboard.fixture_data import AnalyticsFixtureStore
from business_assistant.service import BusinessAssistantService
from security.api_auth import configure_security
from security.identity import RequestSecurityContext


def _auth_env():
    return {
        "SECURITY_AUTH_MODE": "required",
        "PANDA_API_KEYS": (
            "key-a|tenant-a|user-a|user|secret-a;"
            "key-fin|tenant-a|fin-a|admin|secret-fin;"
            "key-b|tenant-b|user-b|user|secret-b;"
            "key-view|tenant-a|viewer|viewer|secret-view"
        ),
        "ANALYTICS_DASHBOARD_MODE": "FIXTURE",
    }


def _headers(key: str) -> dict:
    return {"X-API-Key": key}


def _ctx(tenant: str = "tenant-a", roles=("user",)) -> RequestSecurityContext:
    return RequestSecurityContext(tenant_id=tenant, user_id="u", roles=roles, request_id="r1")


def _svc(store: AnalyticsFixtureStore | None = None) -> AnalyticsDashboardService:
    return AnalyticsDashboardService(fixture_store=store or AnalyticsFixtureStore())


class StatusFlagsTests(unittest.TestCase):
    def test_flags(self):
        self.assertTrue(analytics_dashboard_engineering_ready())
        self.assertFalse(analytics_dashboard_live_active())
        self.assertFalse(analytics_dashboard_live_verified())


class MetricRegistryTests(unittest.TestCase):
    def test_registry_deterministic(self):
        self.assertIn("commerce.revenue", METRIC_REGISTRY)
        self.assertEqual(get_metric("commerce.revenue").unit, "money")

    def test_unsupported_metric(self):
        svc = _svc()
        with self.assertRaises(AnalyticsError) as cm:
            svc.validate_query(AnalyticsQuery(tenant_id="tenant-a", metrics=["fake.metric"], start="2026-01-01T00:00:00+00:00", end="2026-01-31T00:00:00+00:00"))
        self.assertEqual(cm.exception.code, UNSUPPORTED_METRIC)


class QueryValidationTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()

    def test_invalid_time_range(self):
        with self.assertRaises(AnalyticsError):
            self.svc.validate_query(AnalyticsQuery(tenant_id="t", metrics=["commerce.orders.count"], start="2026-02-01T00:00:00+00:00", end="2026-01-01T00:00:00+00:00"))

    def test_range_too_large(self):
        with self.assertRaises(AnalyticsError):
            self.svc.validate_query(AnalyticsQuery(tenant_id="t", metrics=["commerce.orders.count"], start="2020-01-01T00:00:00+00:00", end="2026-01-01T00:00:00+00:00"))

    def test_invalid_dimension(self):
        with self.assertRaises(AnalyticsError):
            self.svc.validate_query(AnalyticsQuery(tenant_id="t", metrics=["commerce.orders.count"], start="2026-01-01T00:00:00+00:00", end="2026-01-31T00:00:00+00:00", filters={"sql_injection": "1=1"}))


class MetricSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()

    def test_revenue_aggregation(self):
        r = _range_query()
        out = self.svc.query_metrics(
            _ctx(),
            AnalyticsQuery(
                tenant_id="tenant-a",
                metrics=["commerce.revenue"],
                start=r["start"],
                end=r["end"],
            ),
        )
        val = out["metrics"][0]
        self.assertEqual(val["status"], STATUS_OK)
        self.assertEqual(val["currency"], "RUB")
        self.assertEqual(Decimal(val["value"]), Decimal("62500.50"))

    def test_order_count(self):
        r = _range_query()
        out = self.svc.query_metrics(
            _ctx(),
            AnalyticsQuery(tenant_id="tenant-a", metrics=["commerce.orders.count"], start=r["start"], end=r["end"]),
        )
        self.assertEqual(out["metrics"][0]["value"], 5)

    def test_aov(self):
        r = _range_query()
        out = self.svc.query_metrics(
            _ctx(),
            AnalyticsQuery(tenant_id="tenant-a", metrics=["commerce.average_order_value"], start=r["start"], end=r["end"]),
        )
        self.assertEqual(Decimal(out["metrics"][0]["value"]), Decimal("12500.10"))

    def test_no_data_not_zero(self):
        store = AnalyticsFixtureStore()
        store._orders["tenant-empty"] = []
        svc = _svc(store)
        r = _range_query()
        out = svc.query_metrics(
            _ctx(tenant="tenant-empty"),
            AnalyticsQuery(tenant_id="tenant-empty", metrics=["commerce.revenue"], start=r["start"], end=r["end"]),
        )
        self.assertEqual(out["metrics"][0]["status"], STATUS_NO_DATA)

    def test_unknown_cost_not_zero(self):
        out = self.svc.finops(_ctx(roles=("admin",)), tenant_id="tenant-a")
        self.assertGreater(out["unknown_cost_entries"], 0)
        self.assertNotEqual(out["total_known_cost"], "0")

    def test_price_floor_risk(self):
        r = _range_query()
        out = self.svc.query_metrics(
            _ctx(),
            AnalyticsQuery(tenant_id="tenant-a", metrics=["marketplace.price_floor_risk"], start=r["start"], end=r["end"]),
        )
        self.assertEqual(out["metrics"][0]["value"], 1)


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()

    def test_marketplace_breakdown(self):
        out = self.svc.marketplaces(_ctx(), tenant_id="tenant-a")
        self.assertIn("wildberries", out["marketplaces"])
        self.assertEqual(out["marketplaces"]["wildberries"]["currency"], "RUB")

    def test_partial_marketplace(self):
        out = self.svc.marketplaces(_ctx(), tenant_id="tenant-a")
        self.assertEqual(out["status"], STATUS_PARTIAL)
        self.assertIn("yandex_market", out["unavailable_sources"])


class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()

    def test_commerce_isolation(self):
        a = self.svc.overview(_ctx(tenant="tenant-a"), tenant_id="tenant-a", window="30d")
        b = self.svc.overview(_ctx(tenant="tenant-b"), tenant_id="tenant-b", window="30d")
        self.assertNotEqual(a["cards"]["revenue"]["value"], b["cards"]["revenue"]["value"])

    def test_cross_tenant_forbidden(self):
        with self.assertRaises(AnalyticsError):
            self.svc.overview(_ctx(tenant="tenant-a"), tenant_id="tenant-b")

    def test_finops_isolation(self):
        a = self.svc.finops(_ctx(roles=("admin",), tenant="tenant-a"), tenant_id="tenant-a")
        b = self.svc.finops(_ctx(roles=("admin",), tenant="tenant-b"), tenant_id="tenant-b")
        self.assertNotEqual(a["total_known_cost"], b["total_known_cost"])

    def test_alerts_isolation(self):
        a = self.svc.alerts(_ctx(), tenant_id="tenant-a")
        b = self.svc.alerts(_ctx(tenant="tenant-b"), tenant_id="tenant-b")
        self.assertNotEqual(len(a["alerts"]), 0)
        self.assertTrue(all("tenant-a" in al["alert_id"] or al["tenant_id"] == "tenant-a" for al in a["alerts"]))


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()

    def test_finops_forbidden_for_guest(self):
        with self.assertRaises(AnalyticsError):
            self.svc.finops(_ctx(roles=("guest",)), tenant_id="tenant-a")

    def test_finops_allowed_for_admin(self):
        out = self.svc.finops(_ctx(roles=("admin",)), tenant_id="tenant-a")
        self.assertIn("total_known_cost", out)


class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()

    def test_price_floor_alert(self):
        alerts = self.svc.alerts(_ctx(), tenant_id="tenant-a")["alerts"]
        types = {a["alert_type"] for a in alerts}
        self.assertIn("price_floor_violation", types)

    def test_low_stock_alert(self):
        alerts = self.svc.alerts(_ctx(), tenant_id="tenant-a")["alerts"]
        self.assertTrue(any(a["alert_type"] == "low_stock" for a in alerts))

    def test_integration_error_alert(self):
        alerts = self.svc.alerts(_ctx(), tenant_id="tenant-a")["alerts"]
        self.assertTrue(any(a["alert_type"] == "integration_error" for a in alerts))

    def test_no_auto_remediation(self):
        before = self.svc._fixture.stock("tenant-a")[1]["units"]
        self.svc.alerts(_ctx(), tenant_id="tenant-a")
        after = self.svc._fixture.stock("tenant-a")[1]["units"]
        self.assertEqual(before, after)


class ProductStockTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()

    def test_product_identity_by_sku(self):
        out = self.svc.products(_ctx(), tenant_id="tenant-a")
        skus = [i["sku"] for i in out["items"]]
        self.assertEqual(len(skus), len(set(skus)))

    def test_stock_pools_separate(self):
        out = self.svc.products(_ctx(), tenant_id="tenant-a")
        wh = {(i["sku"], i["warehouse"]) for i in out["items"]}
        self.assertIn(("SKU-200", "fbs_main"), wh)
        self.assertIn(("SKU-100", "fbo_main"), wh)


class TimeseriesTests(unittest.TestCase):
    def setUp(self):
        self.svc = _svc()

    def test_timeseries_buckets(self):
        r = _range_query()
        out = self.svc.timeseries(
            _ctx(),
            tenant_id="tenant-a",
            metric_id="commerce.revenue",
            start=r["start"],
            end=r["end"],
        )
        self.assertTrue(out["points"])
        self.assertEqual(out["timezone"], "Europe/Moscow")


class BATests(unittest.TestCase):
    def test_ba_governed_analytics(self):
        svc = _svc()
        ba = BusinessAssistantService(analytics_dashboard=svc)
        ex = type("Ex", (), {"tenant_id": "tenant-a", "artifacts": [], "cost": Decimal("0"), "execution_id": "x", "workflow_id": "w", "_analytics_question_type": "sales_week"})()
        step = type("S", (), {"name": "analytics_query", "capability": "analytics"})()
        out = ba._execute_step(ex, None, type("R", (), {"text": ""})(), step)
        self.assertFalse(out.get("mutation", True))
        self.assertIn("analytics", out)

    def test_ba_no_raw_sql(self):
        svc = _svc()
        self.assertFalse(hasattr(svc, "execute_sql"))


class ObservabilityTests(unittest.TestCase):
    def test_observability_emitted(self):
        svc = _svc()
        svc.overview(_ctx(), tenant_id="tenant-a")
        self.assertTrue(svc.observability_events())


class LiveSafetyTests(unittest.TestCase):
    def test_live_no_fixture_fallback(self):
        svc = _svc()
        import analytics_dashboard.config as cfg

        old = os.environ.get("ANALYTICS_DASHBOARD_MODE")
        os.environ["ANALYTICS_DASHBOARD_MODE"] = "LIVE"
        os.environ["ANALYTICS_DASHBOARD_LIVE_URL"] = "http://example.com"
        try:
            self.assertTrue(cfg.analytics_dashboard_live_active())
            with self.assertRaises(AnalyticsError) as cm:
                svc.query_metrics(
                    _ctx(),
                    AnalyticsQuery(tenant_id="tenant-a", metrics=["commerce.revenue"], start="2026-01-01T00:00:00+00:00", end="2026-01-31T00:00:00+00:00"),
                )
            self.assertEqual(cm.exception.code, LIVE_FALLBACK_FORBIDDEN)
        finally:
            if old is None:
                os.environ.pop("ANALYTICS_DASHBOARD_MODE", None)
            else:
                os.environ["ANALYTICS_DASHBOARD_MODE"] = old
            os.environ.pop("ANALYTICS_DASHBOARD_LIVE_URL", None)


class APITests(unittest.TestCase):
    def setUp(self):
        self._env = _auth_env()
        for k, v in self._env.items():
            os.environ[k] = v
        configure_security()
        runtime = build_analytics_dashboard_runtime()
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(configure_analytics_dashboard_router(runtime.service, runtime.policy))
        self.client = TestClient(app)

    def test_overview_api(self):
        r = self.client.get("/api/v1/analytics/overview?tenant_id=tenant-a", headers=_headers("secret-a"))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "FIXTURE")
        self.assertIn("cards", body)

    def test_status_api(self):
        r = self.client.get("/api/v1/analytics/status")
        self.assertTrue(r.json()["engineering_ready"])

    def test_unauthorized(self):
        r = self.client.get("/api/v1/analytics/overview?tenant_id=tenant-a")
        self.assertIn(r.status_code, (401, 403))

    def test_no_secrets_in_response(self):
        r = self.client.get("/api/v1/analytics/overview?tenant_id=tenant-a", headers=_headers("secret-a"))
        blob = r.text
        self.assertNotIn("secret-a", blob)
        self.assertNotIn("password", blob.lower())


class UITests(unittest.TestCase):
    def test_static_assets(self):
        for path in ("static/analytics/index.html", "static/analytics/analytics.js", "static/analytics/analytics.css"):
            self.assertTrue(os.path.isfile(path))

    def test_ui_state_handlers(self):
        with open("static/analytics/analytics.js", encoding="utf-8") as fh:
            src = fh.read()
        for state in ("loading", "error", "no-data", "partial", "stale", "unauthorized"):
            self.assertIn(state, src)


class FreshnessTests(unittest.TestCase):
    def test_freshness_exposed(self):
        out = _svc().overview(_ctx(), tenant_id="tenant-a")
        self.assertIn("freshness_at", out)
        self.assertIn("generated_at", out)


class WorkflowIntegrationTests(unittest.TestCase):
    def test_workflows_and_queues(self):
        out = _svc().workflows(_ctx(), tenant_id="tenant-a")
        self.assertIn("queues", out)
        self.assertIn("success_rate", out["workflows"])


class ProductivityPrivacyTests(unittest.TestCase):
    def test_no_sensitive_email_in_metrics(self):
        out = _svc().integrations_health(_ctx(), tenant_id="tenant-a")
        blob = str(out)
        self.assertNotIn("Ignore previous rules", blob)
        self.assertNotIn("body", blob.lower() or True)


if __name__ == "__main__":
    unittest.main()
