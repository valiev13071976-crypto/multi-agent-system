# Business Assistant API / Chat

## Architecture

```
Client (Web / Telegram / API)
        ↓
/api/v1/business-assistant
        ↓
Auth + RequestSecurityContext
        ↓
BusinessAssistantApiService (interaction boundary)
        ↓
BusinessAssistantService (closed domain core)
        ↓
Integration Activation / ToolGateway / Workflows
```

**This layer is NOT a second chat engine or Business Assistant core.**

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/requests` | Submit business request |
| GET | `/requests/{id}` | Request summary |
| GET | `/requests/{id}/status` | Status + safe plan/progress |
| GET | `/requests/{id}/events` | Ordered progress events |
| GET | `/requests/{id}/result` | Normalized result |
| GET | `/requests/{id}/artifacts` | Tenant-scoped artifacts |
| GET | `/requests/{id}/preview` | Governed write preview |
| POST | `/requests/{id}/approve` | HITL-bound approval |
| POST | `/requests/{id}/reject` | Reject pending action |
| POST | `/requests/{id}/cancel` | Cancel request |

## Request lifecycle

`RECEIVED → VALIDATING → PLANNING → RUNNING → … → COMPLETED`

Governed writes: `WAITING_FOR_APPROVAL → RESUMING → COMPLETED`

## Durability

SQLite store (`ba_api.sqlite`) persists requests, events, conversations, snapshots for recovery.

## Principles

- `tenant_id` / `owner_id` from auth — never trusted from body
- Idempotency via `idempotency_key` per tenant
- LIVE ≠ FIXTURE — inherited from Integration Activation
- Connection ≠ write authorization
- No secrets in events/results

## Non-goals

Web UI, Telegram, Voice, live vendor activation.
