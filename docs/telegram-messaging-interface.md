# Telegram / Messaging Interface

Canonical Telegram transport for Panda Multi-Agent. Telegram is a presentation layer over the closed **Business Assistant API / Chat** — not a second workflow or business engine.

## Architecture

```
Telegram User
  ↓
Telegram Bot Transport (webhook / fixture ingest)
  ↓
telegram_interface/ (normalize, bind, dedup, render)
  ↓
Business Assistant API / Chat
  ↓
Business Assistant → Durable Workflow → ToolGateway → Integration Activation
```

Telegram code does **not** call marketplace, Bitrix, LLM providers, or ToolGateway directly.

## Package layout

| Module | Role |
|--------|------|
| `normalize.py` | Raw Telegram payload → `NormalizedTelegramUpdate` |
| `store.py` | Bindings, update dedup, sessions, callback tokens |
| `service.py` | Orchestration → `BusinessAssistantApiService` |
| `transport.py` | Outbound send + file download (provider/fake) |
| `render.py` | Safe text, preview, result formatting |
| `router.py` | `POST /api/v1/telegram/webhook/{tenant_id}` |
| `runtime.py` | Wiring BA API + store + transport |

## Configuration (names only — no values)

| Variable | Required | Purpose |
|----------|----------|---------|
| `TELEGRAM_INTERFACE_ENABLED` | Optional (default true) | Enable interface |
| `TELEGRAM_BOT_TOKEN` | Production live bot | Outbound Telegram API |
| `TELEGRAM_WEBHOOK_SECRET` | Production | Webhook header verification |
| `TELEGRAM_INTERFACE_DB_PATH` | Optional | SQLite transport state |
| `TELEGRAM_DEFAULT_TENANT` | Optional | Default tenant for single-bot setups |
| `BA_API_DB_PATH` | Shared with BA API | Business state |
| `BA_API_UPLOAD_DIR` | Shared | Attachment storage |

**Fixture/test:** `TELEGRAM_ENABLED=false` uses `FakeTelegramProvider`; fixture ingest at `POST /api/v1/telegram/fixture/updates/{tenant_id}` (disabled in production).

## Secret safety

- Bot token from env only; never logged, stored in DB, or returned in API responses
- Callback tokens are opaque server-side references — no secrets in `callback_data`
- User-facing errors are redacted

## Identity binding

Unknown bindings fail closed (`tgi_binding_required`). A Telegram user bound to another tenant cannot select a tenant via payload (`tgi_tenant_mismatch`). Revoked bindings return `tgi_binding_revoked`; disabled bindings return `tgi_user_disabled`.

Live Telegram network (`TELEGRAM_LIVE_ACTIVE`) remains false until a later human-approved activation. Pre-activation runtime always uses `FakeTelegramProvider`.

See `docs/telegram-pre-activation-runbook.md` (do not execute).

## Webhook

- `POST /api/v1/telegram/webhook/{tenant_id}`
- Validates `X-Telegram-Bot-Api-Secret-Token` when `TELEGRAM_WEBHOOK_SECRET` is set
- Update dedup by `update_id` in `tgi_processed_updates`

## Commands

`/start`, `/help`, `/new`, `/status`, `/cancel` — navigation helpers. Normal business requests use plain language.

## Approval (HITL)

Governed WRITE → preview in Telegram → inline Approve/Reject/Cancel buttons → canonical BA API `approve`/`reject`/`cancel`. Duplicate callbacks are idempotent.

## Tests

```bash
python -m unittest tests.test_telegram_messaging_interface_closure -v
python -m unittest tests.test_business_assistant_api_closure -v
python -m unittest tests.test_web_interface_closure -v
```

Covers: analysis, Excel batch, Ozon read, Bitrix approve, duplicate approval, reject, tenant isolation, callback tampering, file security, restart recovery, webhook auth.

## Engineering vs live

| Flag | Meaning |
|------|---------|
| `TELEGRAM_INTERFACE_ENGINEERING_READY` | Implementation + tests complete |
| `TELEGRAM_LIVE_ACTIVE` | Real bot token + live webhook verified |

Engineering closure does not require a live Telegram bot.

## Deferred

- Live Telegram webhook activation (requires real credentials)
- Artifact binary delivery over Telegram (metadata only until secure download transport exists)
- Production polling mode
- Voice interface
