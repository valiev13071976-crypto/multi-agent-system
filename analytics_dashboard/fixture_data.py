"""Deterministic tenant-scoped fixture analytics data."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal


def _seed_orders() -> dict[str, list[dict]]:
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    d1 = (today - timedelta(days=5)).isoformat()
    d2 = (today - timedelta(days=4)).isoformat()
    d3 = (today - timedelta(days=3)).isoformat()
    d4 = (today - timedelta(days=2)).isoformat()
    d5 = (today - timedelta(days=1)).isoformat()
    base = [
        {"order_id": "ord-1", "date": d1, "marketplace": "wildberries", "channel": "online", "revenue": "15000.00", "currency": "RUB", "units": 3, "sku": "SKU-100", "margin_gross": "4500.00"},
        {"order_id": "ord-2", "date": d2, "marketplace": "ozon", "channel": "online", "revenue": "22000.50", "currency": "RUB", "units": 2, "sku": "SKU-200", "margin_gross": "6600.00"},
        {"order_id": "ord-3", "date": d3, "marketplace": "yandex_market", "channel": "online", "revenue": "8500.00", "currency": "RUB", "units": 1, "sku": "SKU-300", "margin_gross": "1700.00"},
        {"order_id": "ord-4", "date": d4, "marketplace": "wildberries", "channel": "online", "revenue": "12000.00", "currency": "RUB", "units": 2, "sku": "SKU-100", "margin_gross": "3600.00"},
        {"order_id": "ord-5", "date": d5, "marketplace": "ozon", "channel": "online", "revenue": "5000.00", "currency": "RUB", "units": 1, "sku": "SKU-400", "margin_gross": "1000.00"},
    ]
    tenant_b = copy.deepcopy(base)
    for o in tenant_b:
        o["order_id"] = f"b-{o['order_id']}"
        o["revenue"] = str(Decimal(o["revenue"]) * Decimal("0.5"))
        o["margin_gross"] = str(Decimal(o["margin_gross"]) * Decimal("0.5"))
    return {"tenant-a": base, "tenant-b": tenant_b, "default": copy.deepcopy(base)}


def _seed_stock() -> dict[str, list[dict]]:
    base = [
        {"sku": "SKU-100", "marketplace": "wildberries", "warehouse": "fbo_main", "units": 120, "low_threshold": 50},
        {"sku": "SKU-200", "marketplace": "ozon", "warehouse": "fbs_main", "units": 15, "low_threshold": 20},
        {"sku": "SKU-300", "marketplace": "yandex_market", "warehouse": "fby_main", "units": 0, "low_threshold": 5},
        {"sku": "SKU-400", "marketplace": "ozon", "warehouse": "fbs_main", "units": 8, "low_threshold": 10},
    ]
    tenant_b = copy.deepcopy(base)
    for s in tenant_b:
        s["sku"] = f"B-{s['sku']}"
        s["units"] = max(0, s["units"] - 5)
    return {"tenant-a": base, "tenant-b": tenant_b, "default": copy.deepcopy(base)}


def _seed_price_risk() -> dict[str, list[dict]]:
    base = [
        {"sku": "SKU-200", "marketplace": "ozon", "seller_price": "4990.00", "min_allowed_price": "5200.00", "currency": "RUB", "below_floor": True},
        {"sku": "SKU-100", "marketplace": "wildberries", "seller_price": "7500.00", "min_allowed_price": "7000.00", "currency": "RUB", "below_floor": False},
    ]
    tenant_b = copy.deepcopy(base)
    for p in tenant_b:
        p["sku"] = f"B-{p['sku']}"
    return {"tenant-a": base, "tenant-b": tenant_b, "default": copy.deepcopy(base)}


def _seed_runtime() -> dict[str, dict]:
    now = datetime.now(timezone.utc)
    base = {
        "workflows": {"started": 42, "completed": 38, "failed": 4, "hitl_waiting": 2},
        "queues": {"interactive": 3, "normal": 7, "batch": 12},
        "generated_at": now.isoformat(),
        "source_watermark": (now - timedelta(minutes=5)).isoformat(),
    }
    tenant_b = copy.deepcopy(base)
    tenant_b["workflows"]["started"] = 10
    tenant_b["workflows"]["completed"] = 9
    return {"tenant-a": base, "tenant-b": tenant_b, "default": copy.deepcopy(base)}


def _seed_finops() -> dict[str, dict]:
    base = {
        "requests": [
            {"provider": "openai", "model": "gpt-4", "count": 120, "cost": "45.60", "currency": "USD"},
            {"provider": "anthropic", "model": "claude-3", "count": 80, "cost": "32.10", "currency": "USD"},
            {"provider": "unknown", "model": "unknown", "count": 5, "cost": None, "currency": ""},
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    tenant_b = copy.deepcopy(base)
    tenant_b["requests"][0]["count"] = 20
    tenant_b["requests"][0]["cost"] = "8.00"
    return {"tenant-a": base, "tenant-b": tenant_b, "default": copy.deepcopy(base)}


def _seed_integrations() -> dict[str, list[dict]]:
    base = [
        {"provider": "wildberries", "healthy": True, "last_success": "2026-01-20T10:00:00+00:00", "error_rate": "0.01", "mode": "FIXTURE"},
        {"provider": "ozon", "healthy": True, "last_success": "2026-01-20T09:30:00+00:00", "error_rate": "0.02", "mode": "FIXTURE"},
        {"provider": "yandex_market", "healthy": False, "last_success": "2026-01-19T18:00:00+00:00", "error_rate": "0.15", "mode": "FIXTURE", "unavailable_detail": "rate_limited"},
        {"provider": "email", "healthy": True, "last_success": "2026-01-20T11:00:00+00:00", "error_rate": "0.00", "mode": "FIXTURE"},
    ]
    tenant_b = copy.deepcopy(base)
    for i in tenant_b:
        i["provider"] = f"b-{i['provider']}"
    return {"tenant-a": base, "tenant-b": tenant_b, "default": copy.deepcopy(base)}


def _seed_ba() -> dict[str, dict]:
    return {
        "tenant-a": {"requests": 25, "approved": 18, "rejected": 2, "failed": 1},
        "tenant-b": {"requests": 8, "approved": 6, "rejected": 0, "failed": 1},
        "default": {"requests": 0, "approved": 0, "rejected": 0, "failed": 0},
    }


class AnalyticsFixtureStore:
    def __init__(self):
        self._orders = _seed_orders()
        self._stock = _seed_stock()
        self._price_risk = _seed_price_risk()
        self._runtime = _seed_runtime()
        self._finops = _seed_finops()
        self._integrations = _seed_integrations()
        self._ba = _seed_ba()
        self.mode = "FIXTURE"

    def orders(self, tenant_id: str) -> list[dict]:
        tid = tenant_id or "default"
        if tid not in self._orders:
            return []
        return list(self._orders[tid])

    def stock(self, tenant_id: str) -> list[dict]:
        return list(self._stock.get(tenant_id) or self._stock["default"])

    def price_risk(self, tenant_id: str) -> list[dict]:
        return list(self._price_risk.get(tenant_id) or self._price_risk["default"])

    def runtime(self, tenant_id: str) -> dict:
        return dict(self._runtime.get(tenant_id) or self._runtime["default"])

    def finops(self, tenant_id: str) -> dict:
        return dict(self._finops.get(tenant_id) or self._finops["default"])

    def integrations(self, tenant_id: str) -> list[dict]:
        return list(self._integrations.get(tenant_id) or self._integrations["default"])

    def ba(self, tenant_id: str) -> dict:
        return dict(self._ba.get(tenant_id) or self._ba["default"])


GLOBAL_ANALYTICS_FIXTURE = AnalyticsFixtureStore()
