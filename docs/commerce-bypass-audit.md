# Block 11 — E-commerce / Product Platform bypass audit (post P1 patch)

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Agent → Bitrix/CMS direct | PASS | External ops via `FakeCommerceCmsProvider` behind governed service |
| ToolGateway → SideEffectExecutor commerce bridge | PASS | `register_commerce_platform_side_effects()` in `side_effects/runtime.py` |
| CMS stock from trusted inventory | PASS | `ProductPlatformService.cms_update_stock()` reads `InventoryPosition` |
| Raw stock payload bypass | PASS | `ProductPlatformToolAdapter` rejects `stock`/`quantity` args |
| Price apply via decision | PASS | `apply_price_decision()` + SideEffect adapter |
| Capability alignment | PASS | Descriptors use `pricing.write`, `stock.write`, `catalog.write` |
| Bulk repricing durable apply | PASS | `start_bulk_reprice_apply()` + `pp_commerce_jobs` |
| CMS bulk sync durable | PASS | `start_cms_bulk_sync()` + checkpoint |
| Order event sequencing | PASS | `transition_order_with_sequence()` |
| Tenant isolation | PASS | Repository tenant-scoped queries |

## Canonical external write path

ToolGateway → SideEffectExecutor → `CommercePlatformSideEffectAdapter` → `ProductPlatformToolAdapter` → `ProductPlatformService` → provider/repository

## Residual P2

- Live Bitrix credentials
- Live FX provider
- Full ERP/1C integration
- CMS media/content bind (Block 9/10 hooks available, not wired in this patch)
- Governed price rollback execution
