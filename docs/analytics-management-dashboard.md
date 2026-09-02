# Analytics & Management Dashboard

Governed read-model analytics layer for Panda Multi-Agent management visibility.

## Architecture

```
Existing Panda Sources → Analytics Aggregation → Tenant Enforcement → Analytics API → Dashboard UI
```

The analytics layer is a **read model**. Source systems (commerce, marketplace, integrations, workflows, FinOps) remain authoritative.

## Status Flags

| Flag | Engineering closure |
|------|---------------------|
| `ANALYTICS_DASHBOARD_ENGINEERING_READY` | `true` when fixture E2E proven |
| `ANALYTICS_DASHBOARD_LIVE_ACTIVE` | `false` |
| `ANALYTICS_DASHBOARD_LIVE_VERIFIED` | `false` |

**ENGINEERING_READY ≠ LIVE_ACTIVE ≠ LIVE_VERIFIED**

## Package

`analytics_dashboard/` — models, metrics registry, fixture data, service, API router, runtime.

## Metric Registry

Governed metrics include commerce orders/revenue/AOV, price-floor risk, stock, workflow success, AI cost, integration health, BA requests, productivity operations.

Unsupported metrics return `UNSUPPORTED_METRIC`. `NO_DATA` ≠ zero. Unknown cost ≠ zero.

## Money / Currency

Decimal-safe aggregation. Per-currency results; no fabricated FX conversion.

## Marketplace Analytics

Wildberries, Ozon, Yandex Market breakdown with explicit `PARTIAL` when a provider is unavailable.

## Tenant Isolation

All queries tenant-scoped. Cross-tenant requests fail closed.

## Authorization

Reuses existing RBAC. FinOps views require admin/operator roles.

## Freshness / Partial Data

Responses expose `freshness_at`, `generated_at`, `status` (`OK`, `NO_DATA`, `PARTIAL`, `STALE`, `UNAVAILABLE`).

## Alerts

Deterministic signals: price-floor violation, low stock, integration errors, stale snapshot. No auto-remediation.

## API

`/api/v1/analytics/overview`, `/metrics`, `/timeseries`, `/marketplaces`, `/products`, `/integrations`, `/workflows`, `/finops`, `/alerts`, `/status`

## Dashboard UI

`/analytics` — extends Web Interface conventions (`static/analytics/`).

Widget states: LOADING, READY, NO_DATA, PARTIAL, STALE, ERROR, UNAUTHORIZED.

## Business Assistant Access

Step `analytics_query` → governed `AnalyticsDashboardService.ba_query()` — no raw SQL.

## Fixture Mode

Deterministic tenant-scoped fixture data. All responses include `mode: FIXTURE`.

## LIVE Boundary

LIVE mode fails closed; never falls back to FIXTURE.

## Deferred

- Production LIVE data warehouse connection
- Governed export (CSV/XLSX) via Data Intelligence
- Forecasting / ML projections

## Key Files

- `analytics_dashboard/service.py`
- `tests/test_analytics_management_dashboard_closure.py`
