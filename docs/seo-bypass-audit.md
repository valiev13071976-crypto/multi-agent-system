# Block 12 — SEO & Digital Marketing bypass audit (post closure)

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Agent → Search Console SDK direct | PASS | `SearchConsoleService` + fake provider |
| Agent → analytics SDK direct | PASS | `AnalyticsService` + fake provider |
| Agent → arbitrary HTTP/crawler | PASS | Technical audit uses crawl snapshots; `acquisition_service` hook |
| Agent → CMS direct | PASS | `meta_apply` via SideEffectExecutor |
| SEO → direct CMS | PASS | `SeoMarketingSideEffectAdapter` → `SeoMarketingToolAdapter` |
| Fabricated keyword volume | PASS | `NOT_AVAILABLE` when no trusted provider |
| Fabricated performance metrics | PASS | Provider-neutral; fake provider in tests |
| Tenant isolation | PASS | SQLite store tenant-scoped |
| Property binding | PASS | `SeoAccessPolicy.require_property` |
| Generate ≠ apply | PASS | Separate service methods |
| Stale recommendation | PASS | `page.version != rec.page_version` → `SEO_STALE_RECOMMENDATION` |
| SideEffect registration | PASS | `register_seo_marketing_side_effects()` |

## Canonical path

ToolGateway → SideEffectExecutor (writes) → `SeoMarketingSideEffectAdapter` → `SeoMarketingToolAdapter` → `SeoMarketingService` → store/providers

## P2 / deployment-deferred

- Live Google Search Console OAuth
- Live GA4
- Live PageSpeed/CrUX
- Third-party keyword volume (Ahrefs/Semrush)
