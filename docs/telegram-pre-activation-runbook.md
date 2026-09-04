# Telegram pre-activation runbook (Block 22A)

This runbook is documentation only. Do not execute any step from this file as part of Block 22A.

Live activation requires a later human-approved block. After Block 22A:

- `telegram_live_active` = false
- `telegram_live_verified` = false

## PRE-ACTIVATION

Prerequisites:

- Existing `telegram_interface` package is the only Telegram transport.
- Business Assistant API / Panda AI Core is the conversational path. Do not add a second AI core.
- Bot is owned by the Panda operator (not a personal ad-hoc bot for production).
- Production public endpoint is already defined by the platform (do not change DNS here).
- Identity binding is administrative (`register_binding`). Do not auto-register unknown Telegram users.

Required variable **names** (values are not recorded here):

| VARIABLE_NAME | REQUIRED / OPTIONAL |
|---|---|
| `TELEGRAM_BOT_TOKEN` | REQUIRED for live only |
| `TELEGRAM_WEBHOOK_SECRET` | REQUIRED in production when the interface is enabled |
| `TELEGRAM_INTERFACE_ENABLED` | OPTIONAL (default true) |
| `TELEGRAM_ENABLED` | OPTIONAL (must stay false until live is approved) |
| `TELEGRAM_LIVE_ACTIVE` | OPTIONAL (must stay false until live is approved) |
| `TELEGRAM_INTERFACE_DB_PATH` | OPTIONAL |
| `TELEGRAM_DEFAULT_TENANT` | OPTIONAL |
| `TELEGRAM_INTERFACE_ENGINEERING_READY` | OPTIONAL documentation flag |

Webhook vs polling:

- Canonical inbound mode is **webhook**: `POST /api/v1/telegram/webhook/{tenant_id}`
- Header: `X-Telegram-Bot-Api-Secret-Token` compared to `TELEGRAM_WEBHOOK_SECRET`
- Token must not appear in the URL
- **Polling is NOT APPLICABLE** for this activation path. Do not add a second inbound mechanism.

Binding / bootstrap:

1. Create or confirm the Panda user and tenant in the accounts system.
2. Register a server-side binding: Telegram `user_id` + `chat_id` → Panda `tenant_id` + `owner_id`.
3. Unknown users fail closed (`tgi_binding_required`).
4. Cross-tenant webhook paths fail closed (`tgi_tenant_mismatch`).
5. Revoked bindings fail closed (`tgi_binding_revoked`). Disabled bindings fail closed (`tgi_user_disabled`).

## HUMAN APPROVAL

A human owner must explicitly approve live Telegram. Block 22A does not grant that approval.

Approval must confirm:

- Bot ownership
- Production endpoint
- Secret installation plan
- Binding list for the first users
- Rollback owner

## SECRET INSTALLATION

Install secrets through the approved production secret mechanism only.

Never commit values. Never put the bot token in a URL, log line, exception, diagnostic payload, or browser/client response.

This phase is not executed in Block 22A.

## DEPLOYMENT (if separately approved)

Deploy the already-merged Telegram interface. Do not rebuild Telegram. Do not change public endpoints as part of this runbook unless a later block explicitly approves it.

This phase is not executed in Block 22A.

## TELEGRAM REGISTRATION (if separately approved)

Only after human approval and secret installation:

- Register webhook against `POST /api/v1/telegram/webhook/{tenant_id}`
- Send `X-Telegram-Bot-Api-Secret-Token`
- Do not use polling

This phase is not executed in Block 22A. No `setWebhook` / `deleteWebhook` / `getMe` / `getUpdates` / `sendMessage` here.

## BOUNDED LIVE SMOKE (if separately approved)

Minimum smoke after a later approval (not this block):

1. One known bound user sends one text
2. Confirm Panda fixture/BA path responds
3. Confirm unknown user still fails closed
4. Confirm no WRITE bypass (governed actions still HITL)

This phase is not executed in Block 22A.

## ACCEPT / ROLLBACK

Rollback / webhook disable (if live had been registered in a later block):

- Disable `TELEGRAM_LIVE_ACTIVE` and `TELEGRAM_ENABLED`
- Remove webhook registration if it was set (`deleteWebhook` only in that later approved block)
- Keep bindings server-side; revoke compromised bindings
- Rotate `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET` via the secret mechanism if exposure is suspected

Incident stop:

- Set `TELEGRAM_INTERFACE_ENABLED=false` or `TELEGRAM_LIVE_ACTIVE=false`
- Stop processing at the webhook
- Do not attempt live Telegram API calls from this runbook

## Verification checklist (offline / Block 22A)

- [ ] Fixture E2E tests pass
- [ ] Unknown user fail-closed
- [ ] Cross-tenant denied
- [ ] Duplicate update suppressed
- [ ] Malformed / oversized / unsupported types explicit
- [ ] Governed WRITE still HITL + capabilities
- [ ] Secret **names** documented; secret **values** not in git
- [ ] `telegram_live_active=false`
- [ ] `telegram_live_verified=false`
