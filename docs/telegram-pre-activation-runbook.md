# Telegram activation runbook

Documentation only. Do not execute live Telegram operations from this file.

After Block 22B Phase 1A, live flags remain off until a later human-approved phase:

- `telegram_live_active` = false
- `telegram_live_verified` = false

## Flag semantics

| VARIABLE_NAME | Meaning |
|---|---|
| `TELEGRAM_INTERFACE_ENABLED` | Build canonical `telegram_interface` runtime (default true). If false, `POST /api/v1/telegram/webhook/{tenant_id}` stays in OpenAPI and returns 503. |
| `TELEGRAM_LIVE_ACTIVE` | **LIVE AUTHORIZATION.** Does **not** reject inbound webhook updates. |
| `TELEGRAM_ENABLED` | Select `ProductionTelegramProvider` via existing `build_telegram_provider`. |
| Live network | Used only when **both** `TELEGRAM_LIVE_ACTIVE` and `TELEGRAM_ENABLED` are true. Missing token → fail closed. No silent Fake fallback. |
| Unapproved live | Either flag false → `FakeTelegramProvider`. Outbound Bot API with a production provider without `live_network` → `tgi_live_forbidden`. |
| `TELEGRAM_WEBHOOK_SECRET` | Required in production when the interface is enabled, and whenever live network is selected. Header `X-Telegram-Bot-Api-Secret-Token`. Never in the URL. |
| `TELEGRAM_BOT_TOKEN` | Required for live network only. Never logged or returned. |
| `TELEGRAM_INTERFACE_DB_PATH` | Production live: `/data/telegram_interface.sqlite` (must be under `PANDA_DATA_DIR`). Fixture/tests may use temp SQLite. |
| `TELEGRAM_DEFAULT_TENANT` | Optional; code default `tenant-a`. Webhook `{tenant_id}` must match binding tenant. |
| `TELEGRAM_INTERFACE_ENGINEERING_READY` | Documentation only; not read by runtime. |

Canonical webhook (Panda Business Assistant):

`POST /api/v1/telegram/webhook/{tenant_id}`

Do **not** register:

`POST /integrations/telegram/webhook/{tenant_id}`

that B2B path is a separate integration route.

Binding administration (offline-tested; production execution needs a later approval):

`POST /api/v1/telegram/admin/bindings` — authenticated, `operations:write`, tenant-scoped upsert  
`GET /api/v1/telegram/admin/bindings` — readback by ids  
`POST /api/v1/telegram/admin/bindings/status` — `active` / `revoked` / `disabled`

No public self-binding. Unknown Telegram users → `tgi_binding_required`.

## Later operational sequence (do not run in Phase 1A)

Keep these as **separate** human-approved steps:

1. Code CLOSED (this wiring fix)
2. Git fixation (separate request)
3. Separate human-approved deployment
4. Production config verification (names/presence only)
5. Tenant selection
6. Human-approved production binding mutation
7. Human approval for the first Telegram API call
8. **ONE** `getMe`
9. Evaluate
10. `setWebhook` to `POST /api/v1/telegram/webhook/{tenant_id}` with secret header (token never in URL)
11. Verify
12. One inbound smoke
13. One outbound smoke
14. Accept / rollback

Do not combine these into one uncontrolled operation.

## Production config diagnosis (Phase 1 observation; do not set here)

`/ready` `runtime_config=development` because `PANDA_RUNTIME_PROFILE` was unset and `PANDA_ENV` was not `production`. After this code, profile defaults to `single-node-production` **only if** `PANDA_ENV`/`ENVIRONMENT` is `production`/`prod`.

Proposed later non-secret values (human-approved config phase):

| NAME | Proposed |
|---|---|
| `PANDA_ENV` | `production` |
| `PANDA_RUNTIME_PROFILE` | `single-node-production` |
| `PANDA_DATA_DIR` | `/data` |
| `TELEGRAM_INTERFACE_ENABLED` | `true` |
| `TELEGRAM_INTERFACE_DB_PATH` | `/data/telegram_interface.sqlite` |
| `TELEGRAM_LIVE_ACTIVE` | `false` until live approval |
| `TELEGRAM_ENABLED` | `false` until live approval |

`/ready` `production_config=WARN` is expected when backup destination is local and/or `PANDA_ALERT_WEBHOOK_URL` is unset. That is an operator config gap, not a Telegram wiring defect.

## Rollback (not executed here)

- Set `TELEGRAM_LIVE_ACTIVE=false` and `TELEGRAM_ENABLED=false`
- Optionally `TELEGRAM_INTERFACE_ENABLED=false` (webhook returns 503; route remains)
- `deleteWebhook` only in a later approved phase if a webhook had been registered
- Preserve `/data` binding/dedup SQLite
- Do not delete the bot; do not print tokens
