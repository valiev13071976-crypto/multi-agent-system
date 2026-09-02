"""Analytics dashboard aggregation service — read model only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from analytics_dashboard.access import AnalyticsAccessPolicy
from analytics_dashboard.config import (
    DEFAULT_TIMEZONE,
    MAX_DRILLDOWN_LIMIT,
    MAX_TIME_RANGE_DAYS,
    MAX_TIMESERIES_POINTS,
    analytics_dashboard_live_active,
)
from analytics_dashboard.errors import (
    AnalyticsError,
    INVALID_ANALYTICS_QUERY,
    INVALID_TIME_RANGE,
    LIVE_FALLBACK_FORBIDDEN,
    SOURCE_UNAVAILABLE,
    UNSUPPORTED_DIMENSION,
    UNSUPPORTED_METRIC,
)
from analytics_dashboard.fixture_data import AnalyticsFixtureStore, GLOBAL_ANALYTICS_FIXTURE
from analytics_dashboard.metrics import ALLOWED_FILTER_KEYS, ALLOWED_GROUP_BY, METRIC_REGISTRY, get_metric
from analytics_dashboard.models import (
    STATUS_NO_DATA,
    STATUS_NOT_SUPPORTED,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_STALE,
    STATUS_UNAVAILABLE,
    AlertSignal,
    AnalyticsQuery,
    MetricValue,
    TimeSeriesPoint,
)
from security.identity import RequestSecurityContext
from security.tenant import require_tenant_id


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


class AnalyticsDashboardService:
    """Tenant-scoped read-model aggregator over fixture and optional live sources."""

    STALE_THRESHOLD_MINUTES = 60

    def __init__(
        self,
        *,
        fixture_store: AnalyticsFixtureStore | None = None,
        integration_activation=None,
        marketplace=None,
        ops_admin=None,
        access: AnalyticsAccessPolicy | None = None,
    ):
        self._fixture = fixture_store or GLOBAL_ANALYTICS_FIXTURE
        self._activation = integration_activation
        self._marketplace = marketplace
        self._ops_admin = ops_admin
        self._access = access or AnalyticsAccessPolicy()
        self._obs: list[dict] = []

    def observability_events(self) -> list[dict]:
        return list(self._obs)

    def _emit_obs(self, *, tenant_id: str, endpoint: str, status: str, latency_ms: float, metric_id: str = "") -> None:
        self._obs.append(
            {
                "tenant_id": tenant_id,
                "endpoint": endpoint,
                "metric_id": metric_id,
                "status": status,
                "latency_ms": latency_ms,
                "mode": "FIXTURE" if not analytics_dashboard_live_active() else "LIVE",
            }
        )

    def validate_query(self, query: AnalyticsQuery) -> None:
        if not query.metrics:
            raise AnalyticsError(INVALID_ANALYTICS_QUERY, "metrics_required")
        for mid in query.metrics:
            if mid not in METRIC_REGISTRY:
                raise AnalyticsError(UNSUPPORTED_METRIC, mid)
        for key in query.filters:
            if key not in ALLOWED_FILTER_KEYS:
                raise AnalyticsError(UNSUPPORTED_DIMENSION, key)
        for dim in query.group_by:
            if dim not in ALLOWED_GROUP_BY:
                raise AnalyticsError(UNSUPPORTED_DIMENSION, dim)
        try:
            start = _parse_dt(query.start)
            end = _parse_dt(query.end)
        except ValueError as exc:
            raise AnalyticsError(INVALID_TIME_RANGE, "invalid_datetime") from exc
        if end <= start:
            raise AnalyticsError(INVALID_TIME_RANGE, "end_before_start")
        if (end - start).days > MAX_TIME_RANGE_DAYS:
            raise AnalyticsError(INVALID_TIME_RANGE, "range_too_large")
        if query.limit > MAX_DRILLDOWN_LIMIT or query.limit < 1:
            raise AnalyticsError(INVALID_ANALYTICS_QUERY, "invalid_limit")

    def overview(self, ctx: RequestSecurityContext, *, tenant_id: str, window: str = "7d") -> dict:
        started = _utc_now()
        tenant = require_tenant_id(tenant_id)
        self._access.require_read(ctx, tenant_id=tenant)
        if analytics_dashboard_live_active() and not self._live_configured():
            raise AnalyticsError(SOURCE_UNAVAILABLE, "live_not_configured")

        orders = self._filtered_orders(tenant, window)
        revenue = self._sum_revenue(orders)
        order_count = len(orders)
        aov = self._average_order_value(orders)
        stock = self._fixture.stock(tenant)
        low_stock = [s for s in stock if s["units"] <= s["low_threshold"]]
        price_risk = [p for p in self._fixture.price_risk(tenant) if p.get("below_floor")]
        runtime = self._fixture.runtime(tenant)
        integrations = self._fixture.integrations(tenant)
        unhealthy = [i for i in integrations if not i.get("healthy")]
        finops = self._fixture.finops(tenant)
        unknown_cost = any(r.get("cost") is None for r in finops.get("requests", []))

        freshness = runtime.get("source_watermark") or runtime.get("generated_at") or ""
        stale = self._is_stale(freshness)
        mp_partial = any(i.get("unavailable_detail") for i in integrations)

        cards = {
            "orders": {"value": order_count, "status": STATUS_OK if order_count else STATUS_NO_DATA, "unit": "count"},
            "revenue": {"value": str(revenue), "status": STATUS_OK if revenue else STATUS_NO_DATA, "currency": "RUB", "unit": "money"},
            "average_order_value": {"value": str(aov) if aov is not None else None, "status": STATUS_OK if aov else STATUS_NO_DATA, "currency": "RUB"},
            "low_stock_skus": {"value": len(low_stock), "status": STATUS_OK},
            "price_floor_risk": {"value": len(price_risk), "status": STATUS_OK},
            "workflow_success_rate": {
                "value": self._workflow_success_rate(runtime),
                "status": STATUS_OK,
            },
            "integration_health": {
                "value": len(integrations) - len(unhealthy),
                "total": len(integrations),
                "status": STATUS_PARTIAL if mp_partial else STATUS_OK,
            },
            "ai_cost_usd": {
                "value": self._known_ai_cost(finops),
                "status": STATUS_UNAVAILABLE if unknown_cost else STATUS_OK,
                "currency": "USD",
            },
            "hitl_waiting": {"value": runtime.get("workflows", {}).get("hitl_waiting", 0), "status": STATUS_OK},
        }

        out = {
            "tenant_id": tenant,
            "window": window,
            "timezone": DEFAULT_TIMEZONE,
            "mode": "LIVE" if analytics_dashboard_live_active() else "FIXTURE",
            "live": analytics_dashboard_live_active(),
            "cards": cards,
            "freshness_at": freshness,
            "generated_at": _utc_now().isoformat(),
            "status": STATUS_STALE if stale else (STATUS_PARTIAL if mp_partial else STATUS_OK),
            "warnings": (["unknown_ai_cost_present"] if unknown_cost else []) + (["stale_snapshot"] if stale else []),
        }
        self._emit_obs(tenant_id=tenant, endpoint="overview", status=out["status"], latency_ms=(_utc_now() - started).total_seconds() * 1000)
        return out

    def query_metrics(self, ctx: RequestSecurityContext, query: AnalyticsQuery) -> dict:
        started = _utc_now()
        tenant = require_tenant_id(query.tenant_id)
        self._access.require_read(ctx, tenant_id=tenant)
        self.validate_query(query)
        if analytics_dashboard_live_active():
            raise AnalyticsError(LIVE_FALLBACK_FORBIDDEN, "live_analytics_not_implemented")

        values: list[MetricValue] = []
        for mid in query.metrics:
            if mid == "ai.cost":
                self._access.require_finops(ctx, tenant_id=tenant)
            values.extend(self._compute_metric(tenant, mid, query))

        out = {
            "tenant_id": tenant,
            "metrics": [self._metric_to_dict(v) for v in values],
            "time_range": {"start": query.start, "end": query.end, "timezone": query.timezone},
            "generated_at": _utc_now().isoformat(),
            "mode": "FIXTURE",
            "live": False,
        }
        self._emit_obs(tenant_id=tenant, endpoint="metrics", status=STATUS_OK, latency_ms=(_utc_now() - started).total_seconds() * 1000, metric_id=",".join(query.metrics))
        return out

    def timeseries(self, ctx: RequestSecurityContext, *, tenant_id: str, metric_id: str, start: str, end: str, granularity: str = "day") -> dict:
        started = _utc_now()
        tenant = require_tenant_id(tenant_id)
        self._access.require_read(ctx, tenant_id=tenant)
        if metric_id not in METRIC_REGISTRY:
            raise AnalyticsError(UNSUPPORTED_METRIC, metric_id)
        q = AnalyticsQuery(tenant_id=tenant, metrics=[metric_id], start=start, end=end, granularity=granularity)
        self.validate_query(q)

        points: list[TimeSeriesPoint] = []
        if metric_id == "commerce.revenue":
            orders = self._orders_in_range(tenant, start, end)
            buckets = self._day_buckets(orders, "revenue")
            for bstart, total in buckets.items():
                points.append(TimeSeriesPoint(bucket_start=bstart, value=str(total), status=STATUS_OK if total else STATUS_NO_DATA))
        elif metric_id == "commerce.orders.count":
            orders = self._orders_in_range(tenant, start, end)
            buckets: dict[str, int] = {}
            for o in orders:
                buckets[o["date"]] = buckets.get(o["date"], 0) + 1
            for bstart, cnt in sorted(buckets.items()):
                points.append(TimeSeriesPoint(bucket_start=bstart, value=str(cnt), status=STATUS_OK))
        else:
            points.append(TimeSeriesPoint(bucket_start=start, value="", status=STATUS_NOT_SUPPORTED))

        if len(points) > MAX_TIMESERIES_POINTS:
            points = points[:MAX_TIMESERIES_POINTS]

        out = {
            "tenant_id": tenant,
            "metric_id": metric_id,
            "granularity": granularity,
            "timezone": DEFAULT_TIMEZONE,
            "points": [{"bucket_start": p.bucket_start, "value": p.value, "status": p.status} for p in points],
            "generated_at": _utc_now().isoformat(),
            "mode": "FIXTURE",
        }
        self._emit_obs(tenant_id=tenant, endpoint="timeseries", status=STATUS_OK, latency_ms=(_utc_now() - started).total_seconds() * 1000, metric_id=metric_id)
        return out

    def marketplaces(self, ctx: RequestSecurityContext, *, tenant_id: str, window: str = "30d") -> dict:
        tenant = require_tenant_id(tenant_id)
        self._access.require_read(ctx, tenant_id=tenant)
        orders = self._filtered_orders(tenant, window)
        integrations = self._fixture.integrations(tenant)
        by_mp: dict[str, dict] = {}
        unavailable: list[str] = []
        for mp in ("wildberries", "ozon", "yandex_market"):
            mp_orders = [o for o in orders if o["marketplace"] == mp]
            integ = next((i for i in integrations if i["provider"] == mp), None)
            if integ and integ.get("unavailable_detail"):
                unavailable.append(mp)
            rev = self._sum_revenue(mp_orders)
            by_mp[mp] = {
                "marketplace": mp,
                "orders": len(mp_orders),
                "revenue": str(rev),
                "currency": "RUB",
                "status": STATUS_UNAVAILABLE if integ and integ.get("unavailable_detail") else (STATUS_NO_DATA if not mp_orders else STATUS_OK),
                "integration_healthy": bool(integ.get("healthy")) if integ else False,
                "limitations": ("provider_specific_fields_may_differ",),
            }
        return {
            "tenant_id": tenant,
            "marketplaces": by_mp,
            "unavailable_sources": unavailable,
            "status": STATUS_PARTIAL if unavailable else STATUS_OK,
            "generated_at": _utc_now().isoformat(),
            "mode": "FIXTURE",
        }

    def products(self, ctx: RequestSecurityContext, *, tenant_id: str, limit: int = 50, offset: int = 0) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._access.require_read(ctx, tenant_id=tenant)
        limit = min(max(1, limit), MAX_DRILLDOWN_LIMIT)
        stock = self._fixture.stock(tenant)[offset : offset + limit]
        risk = {p["sku"]: p for p in self._fixture.price_risk(tenant)}
        items = []
        for s in stock:
            r = risk.get(s["sku"], {})
            items.append(
                {
                    "sku": s["sku"],
                    "marketplace": s["marketplace"],
                    "warehouse": s["warehouse"],
                    "stock_units": s["units"],
                    "low_stock": s["units"] <= s["low_threshold"],
                    "price_floor_risk": bool(r.get("below_floor")),
                    "provider_offer_id": f"{s['marketplace']}:{s['sku']}",
                }
            )
        return {"tenant_id": tenant, "items": items, "limit": limit, "offset": offset, "total": len(self._fixture.stock(tenant)), "mode": "FIXTURE"}

    def integrations_health(self, ctx: RequestSecurityContext, *, tenant_id: str) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._access.require_read(ctx, tenant_id=tenant)
        items = []
        for row in self._fixture.integrations(tenant):
            items.append(
                {
                    "provider": row["provider"],
                    "healthy": row.get("healthy"),
                    "error_rate": row.get("error_rate"),
                    "last_success": row.get("last_success"),
                    "mode": row.get("mode", "FIXTURE"),
                    "engineering_ready": True,
                    "live_active": False,
                    "live_verified": False,
                    "status": STATUS_UNAVAILABLE if row.get("unavailable_detail") else STATUS_OK,
                }
            )
        if self._activation is not None:
            for u in self._activation.usage_events(tenant_id=tenant)[:20]:
                items.append({"provider": u.get("provider"), "operation": u.get("operation"), "source": "activation_usage", "mode": "FIXTURE"})
        return {"tenant_id": tenant, "integrations": items, "generated_at": _utc_now().isoformat(), "mode": "FIXTURE"}

    def workflows(self, ctx: RequestSecurityContext, *, tenant_id: str) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._access.require_read(ctx, tenant_id=tenant)
        rt = self._fixture.runtime(tenant)
        wf = rt.get("workflows", {})
        queues = rt.get("queues", {})
        success_rate = self._workflow_success_rate(rt)
        return {
            "tenant_id": tenant,
            "workflows": {**wf, "success_rate": success_rate},
            "queues": queues,
            "freshness_at": rt.get("source_watermark"),
            "mode": "FIXTURE",
        }

    def finops(self, ctx: RequestSecurityContext, *, tenant_id: str) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._access.require_finops(ctx, tenant_id=tenant)
        data = self._fixture.finops(tenant)
        known = Decimal("0")
        unknown_count = 0
        by_provider: dict[str, Decimal] = {}
        for r in data.get("requests", []):
            if r.get("cost") is None:
                unknown_count += 1
                continue
            amt = Decimal(str(r["cost"]))
            known += amt
            by_provider[r["provider"]] = by_provider.get(r["provider"], Decimal("0")) + amt
        return {
            "tenant_id": tenant,
            "total_known_cost": str(known),
            "currency": "USD",
            "unknown_cost_entries": unknown_count,
            "by_provider": {k: str(v) for k, v in by_provider.items()},
            "status": STATUS_PARTIAL if unknown_count else STATUS_OK,
            "generated_at": data.get("generated_at"),
            "mode": "FIXTURE",
        }

    def alerts(self, ctx: RequestSecurityContext, *, tenant_id: str) -> dict:
        tenant = require_tenant_id(tenant_id)
        self._access.require_read(ctx, tenant_id=tenant)
        signals = self._build_alerts(tenant)
        return {
            "tenant_id": tenant,
            "alerts": [self._alert_to_dict(a) for a in signals],
            "generated_at": _utc_now().isoformat(),
            "mode": "FIXTURE",
        }

    def ba_query(self, ctx: RequestSecurityContext, *, tenant_id: str, question_type: str) -> dict:
        """Governed BA analytics access — no raw SQL."""
        tenant = require_tenant_id(tenant_id)
        self._access.require_read(ctx, tenant_id=tenant)
        if question_type == "sales_week":
            return self.overview(ctx, tenant_id=tenant, window="7d")
        if question_type == "marketplace_revenue":
            return self.marketplaces(ctx, tenant_id=tenant, window="30d")
        if question_type == "price_floor":
            risk = [p for p in self._fixture.price_risk(tenant) if p.get("below_floor")]
            return {"tenant_id": tenant, "price_floor_risk": risk, "mode": "FIXTURE", "mutation": False}
        if question_type == "low_stock":
            stock = [s for s in self._fixture.stock(tenant) if s["units"] <= s["low_threshold"]]
            return {"tenant_id": tenant, "low_stock": stock, "mode": "FIXTURE", "mutation": False}
        if question_type == "finops_month":
            return self.finops(ctx, tenant_id=tenant)
        if question_type == "integration_errors":
            bad = [i for i in self._fixture.integrations(tenant) if not i.get("healthy")]
            return {"tenant_id": tenant, "unhealthy_integrations": bad, "mode": "FIXTURE", "mutation": False}
        raise AnalyticsError(UNSUPPORTED_METRIC, question_type)

    def _live_configured(self) -> bool:
        return False

    def _filtered_orders(self, tenant: str, window: str) -> list[dict]:
        days = {"7d": 7, "30d": 30, "24h": 1}.get(window, 30)
        cutoff = (_utc_now() - timedelta(days=days)).date().isoformat()
        return [o for o in self._fixture.orders(tenant) if o["date"] >= cutoff]

    def _orders_in_range(self, tenant: str, start: str, end: str) -> list[dict]:
        s = _parse_dt(start).date().isoformat()
        e = _parse_dt(end).date().isoformat()
        return [o for o in self._fixture.orders(tenant) if s <= o["date"] <= e]

    def _sum_revenue(self, orders: list[dict], currency: str | None = None) -> Decimal:
        total = Decimal("0")
        for o in orders:
            if currency and o.get("currency") != currency:
                continue
            total += Decimal(str(o.get("revenue") or 0))
        return total

    def _average_order_value(self, orders: list[dict]) -> Decimal | None:
        if not orders:
            return None
        return (self._sum_revenue(orders) / Decimal(len(orders))).quantize(Decimal("0.01"))

    def _day_buckets(self, orders: list[dict], field: str) -> dict[str, Decimal]:
        buckets: dict[str, Decimal] = {}
        for o in orders:
            key = o["date"]
            val = Decimal(str(o.get(field if field != "revenue" else "revenue") or 0))
            buckets[key] = buckets.get(key, Decimal("0")) + val
        return buckets

    def _workflow_success_rate(self, runtime: dict) -> str:
        wf = runtime.get("workflows", {})
        started = wf.get("started", 0)
        completed = wf.get("completed", 0)
        if not started:
            return ""
        return str((Decimal(completed) / Decimal(started)).quantize(Decimal("0.0001")))

    def _known_ai_cost(self, finops: dict) -> str | None:
        total = Decimal("0")
        any_known = False
        for r in finops.get("requests", []):
            if r.get("cost") is None:
                continue
            any_known = True
            total += Decimal(str(r["cost"]))
        return str(total) if any_known else None

    def _is_stale(self, freshness_at: str) -> bool:
        if not freshness_at:
            return False
        try:
            ts = _parse_dt(freshness_at)
        except ValueError:
            return False
        return (_utc_now() - ts) > timedelta(minutes=self.STALE_THRESHOLD_MINUTES)

    def _compute_metric(self, tenant: str, metric_id: str, query: AnalyticsQuery) -> list[MetricValue]:
        now = _utc_now().isoformat()
        mp_filter = query.filters.get("marketplace")
        if metric_id == "commerce.orders.count":
            orders = self._orders_in_range(tenant, query.start, query.end)
            if mp_filter:
                orders = [o for o in orders if o["marketplace"] == mp_filter]
            return [MetricValue(metric_id=metric_id, value=len(orders), unit="count", status=STATUS_OK if orders else STATUS_NO_DATA, generated_at=now, source="fixture_commerce")]
        if metric_id == "commerce.revenue":
            orders = self._orders_in_range(tenant, query.start, query.end)
            if mp_filter:
                orders = [o for o in orders if o["marketplace"] == mp_filter]
            rev = self._sum_revenue(orders)
            return [MetricValue(metric_id=metric_id, value=str(rev), unit="money", currency="RUB", status=STATUS_OK if orders else STATUS_NO_DATA, generated_at=now)]
        if metric_id == "commerce.average_order_value":
            orders = self._orders_in_range(tenant, query.start, query.end)
            aov = self._average_order_value(orders)
            return [MetricValue(metric_id=metric_id, value=str(aov) if aov is not None else None, unit="money", currency="RUB", status=STATUS_OK if aov else STATUS_NO_DATA, generated_at=now)]
        if metric_id == "commerce.stock.units":
            stock = self._fixture.stock(tenant)
            total = sum(s["units"] for s in stock)
            return [MetricValue(metric_id=metric_id, value=total, unit="count", status=STATUS_OK, generated_at=now)]
        if metric_id == "marketplace.price_floor_risk":
            risk = [p for p in self._fixture.price_risk(tenant) if p.get("below_floor")]
            return [MetricValue(metric_id=metric_id, value=len(risk), unit="count", status=STATUS_OK, generated_at=now)]
        if metric_id == "ai.cost":
            fin = self._fixture.finops(tenant)
            known = self._known_ai_cost(fin)
            unknown = any(r.get("cost") is None for r in fin.get("requests", []))
            return [MetricValue(metric_id=metric_id, value=known, unit="money", currency="USD", status=STATUS_UNAVAILABLE if unknown and not known else STATUS_OK, warnings=("unknown_cost_entries",) if unknown else (), generated_at=now)]
        if metric_id == "business_assistant.requests":
            return [MetricValue(metric_id=metric_id, value=self._fixture.ba(tenant).get("requests", 0), unit="count", status=STATUS_OK, generated_at=now)]
        if metric_id.startswith("productivity."):
            return [MetricValue(metric_id=metric_id, value=0, unit="count", status=STATUS_NO_DATA, generated_at=now, source="integration_activation")]
        return [MetricValue(metric_id=metric_id, value=None, status=STATUS_NOT_SUPPORTED, generated_at=now)]

    def _build_alerts(self, tenant: str) -> list[AlertSignal]:
        now = _utc_now().isoformat()
        alerts: list[AlertSignal] = []
        for p in self._fixture.price_risk(tenant):
            if p.get("below_floor"):
                alerts.append(
                    AlertSignal(
                        alert_id=f"price-floor-{p['sku']}",
                        alert_type="price_floor_violation",
                        severity="CRITICAL",
                        tenant_id=tenant,
                        domain="marketplace",
                        message=f"{p['sku']} below minimum allowed price on {p['marketplace']}",
                        evidence={"sku": p["sku"], "seller_price": p["seller_price"], "min_allowed_price": p["min_allowed_price"]},
                        timestamp=now,
                    )
                )
        for s in self._fixture.stock(tenant):
            if s["units"] <= s["low_threshold"]:
                alerts.append(
                    AlertSignal(
                        alert_id=f"low-stock-{s['sku']}",
                        alert_type="low_stock",
                        severity="WARNING",
                        tenant_id=tenant,
                        domain="commerce",
                        message=f"Low stock for {s['sku']} ({s['units']} units)",
                        evidence={"sku": s["sku"], "units": s["units"], "warehouse": s["warehouse"]},
                        timestamp=now,
                    )
                )
        for i in self._fixture.integrations(tenant):
            if not i.get("healthy"):
                alerts.append(
                    AlertSignal(
                        alert_id=f"integration-{i['provider']}",
                        alert_type="integration_error",
                        severity="WARNING",
                        tenant_id=tenant,
                        domain="integrations",
                        message=f"Integration {i['provider']} unhealthy",
                        evidence={"provider": i["provider"], "error_rate": i.get("error_rate")},
                        timestamp=now,
                    )
                )
        rt = self._fixture.runtime(tenant)
        if self._is_stale(rt.get("source_watermark", "")):
            alerts.append(
                AlertSignal(
                    alert_id="stale-data",
                    alert_type="stale_snapshot",
                    severity="INFO",
                    tenant_id=tenant,
                    domain="platform",
                    message="Analytics snapshot is stale",
                    evidence={"freshness_at": rt.get("source_watermark")},
                    timestamp=now,
                )
            )
        return alerts

    @staticmethod
    def _metric_to_dict(v: MetricValue) -> dict:
        return {
            "metric_id": v.metric_id,
            "value": v.value,
            "unit": v.unit,
            "currency": v.currency,
            "status": v.status,
            "dimensions": v.dimensions,
            "source": v.source,
            "freshness_at": v.freshness_at,
            "generated_at": v.generated_at,
            "partial": v.partial,
            "warnings": list(v.warnings),
        }

    @staticmethod
    def _alert_to_dict(a: AlertSignal) -> dict:
        return {
            "alert_id": a.alert_id,
            "alert_type": a.alert_type,
            "severity": a.severity,
            "tenant_id": a.tenant_id,
            "domain": a.domain,
            "message": a.message,
            "evidence": a.evidence,
            "timestamp": a.timestamp,
            "status": a.status,
        }
