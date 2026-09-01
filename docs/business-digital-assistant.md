# Business / Digital Assistant

## Architecture

Final applied orchestration layer over closed Panda platforms. **Not** a new Workflow Engine, Tool Registry, Excel parser, Commerce/Marketplace/SEO/Content core.

```
User BusinessRequest
        ↓
 Intent + Constraints (deterministic)
        ↓
 BusinessPlan (recipe-aided, bounded, fingerprinted)
        ↓
 validate (deps/cycle/capabilities/read-only)
        ↓
 Execute READ/ANALYZE/GENERATE/PREPARE
        ↓
 Preview + HITL approval boundary
        ↓
 WRITE only after approval bind (fingerprint)
        ↓
 Verify / Summary / Evidence
```

## Reused platforms

| Concern | Canonical owner |
|---------|-----------------|
| Durable execution / checkpoint | `workflow/` |
| Run identity | `workflow/run_envelope.py` |
| HITL / approvals | `hitl/`, `autonomy/approval.py` |
| Tools | Tool Platform descriptors/gateway |
| Excel/Data | `data_intel/` |
| Documents | `documents/` |
| Content | `content_intel/` |
| Media | `product_media/` |
| SEO | `seo_marketing/` |
| Commerce | `commerce/product_platform/` |
| Marketplace economics | `marketplace/` |
| FinOps | `finops/` |

## Public API

`BusinessAssistantService`:

- `submit_request` / `build_plan` / `validate_plan` / `plan_preview`
- `execute` / `resume` / `get_status` / `get_preview`
- `approve` / `reject` / `cancel` / `get_result`

## HITL / Preview

High-impact writes require:

1. Preview with plan fingerprint + artifact checksum  
2. Approval bound to fingerprint  
3. Material plan/source change → `BA_APPROVAL_STALE` / `BA_STALE_PREVIEW`

**"Show me before publication"** → prepare + `WAITING_FOR_APPROVAL` — **no publication write**.

## Recipes

`SUPPLIER_PRICE_ANALYSIS`, `PRODUCT_LAUNCH_PREPARATION`, `MARKETPLACE_PROFITABILITY_REVIEW`, `SEO_SITE_REVIEW`, `DOCUMENT_COMPARISON`, `CUSTOMER_FOLLOWUP_PREPARATION`, `BUSINESS_DAILY_REPORT`

Templates are planning aids only.

## Security

- Tenant fail-closed  
- Untrusted document/email/web injection cannot escalate capabilities  
- No raw secrets  
- Read-only requests strip WRITE steps  
- Ambiguous product matches never auto-apply  
- Loop prevention via `ActionLedger` causation ACK  

## Fixture vs live

Executions default `mode=FIXTURE`. Unconfigured Bitrix/1C/email → `BA_CONNECTOR_NOT_CONFIGURED` / BLOCKED — never fake live success.

## Channel attachment

Telegram / voice / UI are transports. They submit the same `BusinessRequest`. No channel-specific business core.

## Tests

`tests/test_business_assistant_closure.py`

## Out of scope

Live connectors, Telegram bot, voice UI, production deploy, commit/push.
