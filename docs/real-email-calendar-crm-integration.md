# Real Email / Calendar / CRM Integration

Engineering closure for governed productivity integrations in Panda Multi-Agent.

## Architecture

```
User / Business Assistant
        ↓
Durable Workflow
        ↓
Authorization / Capability Policy
        ↓
HITL when required
        ↓
ToolGateway (IntegrationActivationService.execute_via_gateway)
        ↓
Integration Activation
        ↓
Email | Calendar | CRM Adapter
        ↓
External Provider (FIXTURE | LIVE)
```

Composio (if configured) remains an optional broker under Integration Activation — never a bypass for capability policy, HITL, tenant isolation, or idempotency.

## Status Flags

| Flag | Engineering closure value |
|------|---------------------------|
| `EMAIL_CALENDAR_CRM_ENGINEERING_READY` | `true` when fixture E2E proven |
| `EMAIL_LIVE_ACTIVE` | `false` — no live credentials |
| `EMAIL_LIVE_VERIFIED` | `false` |
| `CALENDAR_LIVE_ACTIVE` | `false` |
| `CALENDAR_LIVE_VERIFIED` | `false` |
| `CRM_LIVE_ACTIVE` | `false` |
| `CRM_LIVE_VERIFIED` | `false` |

**ENGINEERING_READY ≠ LIVE_ACTIVE ≠ LIVE_VERIFIED**

## Packages

| Package | Responsibility |
|---------|----------------|
| `integrations/email/` | Mailbox search/read, thread, draft, governed send |
| `integrations/calendar/` | Calendars, events, free/busy, governed mutations |
| `integrations/crm/` | Contacts, leads, deals, patch semantics, duplicate policy |
| `integrations/productivity/` | Cross-domain status flags |

Each package provides: `config`, `catalog`, `mapping`, `errors`, `fixture_adapter`, `live_adapter`, optional `client`.

## Execution Modes

- **FIXTURE** — deterministic, network-free; all fixture responses include `mode: FIXTURE`, `live: false`
- **LIVE** — dormant adapters; fail closed without configuration; **never** fall back to FIXTURE

## Connection / Auth

- Tenant-scoped `secret:` credential refs only
- `tenant_id`, `owner_id`, `connection_id`, provider identity modeled explicitly
- Secrets never logged; `assert_no_secrets_in_evidence()` guards ActionLedger

## Email

### Canonical model

Normalized messages preserve: `message_id`, `thread_id`, `mailbox`, `from`, `to/cc/bcc`, `subject`, `body`, timestamps, direction, attachments metadata, labels, read state.

### Read / search

Bounded search with pagination (`page`, `page_size`, `max_pages`). No unbounded mailbox dump.

### Draft vs send

- `email.draft.write` → `DRAFT_CREATED` (never reported as SENT)
- `email.send` → governed write with approval + idempotency

### Recipient / attachment safety

- Empty/malformed/ambiguous recipients → fail closed
- Attachments via tenant-scoped refs only: `file:{tenant_id}:...`
- Path traversal and cross-tenant attachment refs rejected

### Writes

Flow: intent → capability → HITL → fingerprint → idempotency → adapter → evidence.

`reconcile_send` supports post-timeout read-back without duplicate send.

## Calendar

### Canonical model

Events preserve: `event_id`, `calendar_id`, organizer, attendees, title, description, location, start/end, timezone, all-day, recurrence metadata, status.

### Timezone

Naive timed events without timezone fail closed (`CalendarTimezoneError`). Offset-bearing starts may default timezone only when unambiguous.

### Availability

`calendar.availability.read` → free/busy only; no mutation; bounded windows.

### Writes

- `calendar.event.create` / `update` / `cancel` — separate capabilities
- Attendee changes bound in approval fingerprint
- `reconcile_event` for uncertain-write recovery

## CRM

### Canonical objects

Contacts, leads, deals; activities supported via `crm.activity.write`.

### Identity

Provider object IDs required — display names alone never conflate records (e.g. two "Ivan Petrov" contacts).

### PATCH semantics

Omitted fields are not cleared; explicit null/clear distinguished where supported.

### Duplicate policy

`DUPLICATE_CANDIDATE` / `AMBIGUOUS_MATCH` — no silent auto-merge.

### Destructive ops

DELETE/MERGE → `UNSUPPORTED_CAPABILITY`.

`reconcile_contact` supports uncertain-write recovery.

## Cross-System Orchestration

All flows via Panda BA/workflow — **no adapter-to-adapter calls**:

| Step | Flow |
|------|------|
| `crm_lead_to_email_draft` | CRM lead READ → email draft prep |
| `crm_contact_to_calendar_prep` | CRM contact READ → calendar event prep |
| `email_to_crm_update_prep` | Email READ → CRM update prep |

## Capabilities

Granular capabilities registered in `integrations/activation/providers.py`:

- Email: `email.read`, `email.draft.write`, `email.send`
- Calendar: `calendar.read`, `calendar.availability.read`, `calendar.event.create/update/cancel`
- CRM: `crm.contact.read/write`, `crm.lead.read`, `crm.deal.read`, `crm.activity.write`

READ never grants WRITE. Draft never grants send.

## HITL / Fingerprints

Approval binds material fields:

- **Email**: account, to/cc/bcc, subject, body hash, attachments, thread
- **Calendar**: calendar, start/end, timezone, attendees, title, operation
- **CRM**: object type, ID, changed fields, operation

Material change after approval requires new approval when policy requires.

## Idempotency / Uncertain Writes

All external writes require idempotency keys. Uncertain outcomes raise `INTEGRATION_UNCERTAIN_WRITE_OUTCOME` — no blind retry. Reconcile operations verify provider state.

## Verification / Pagination / Rate Limits

- Write → reconcile → `VERIFIED` | `MISMATCH` | `UNKNOWN`
- Bounded pagination with duplicate-page guards
- Rate limits normalized to `IntegrationRateLimitedError`

## Prompt Injection

Email bodies, calendar descriptions, CRM notes treated as untrusted data. BA flags injection phrases; zero unauthorized tool execution or capability escalation.

## Observability / ActionLedger / FinOps

Every governed write emits integration evidence and usage events. FinOps via BA `_emit_cost` on orchestration steps.

## Durable Recovery

`reconcile_send`, `reconcile_event`, `reconcile_contact` prove restart-safe verification per domain.

## LIVE Activation Requirements

1. Set provider-specific env (`EMAIL_INTEGRATION_MODE=LIVE`, etc.)
2. Configure OAuth/API credentials via tenant secret refs
3. Verify connection via Integration Activation
4. Separate LIVE verification gate before production writes

## Deferred

- Real webhook ingestion (polling/sync ready)
- CRM delete/merge
- Production email/calendar/CRM mutation during engineering closure

## Key Files

- `integrations/activation/service.py` — gateway wiring
- `integrations/activation/providers.py` — capability registry
- `business_assistant/service.py` — orchestration steps
- `tests/test_real_email_calendar_crm_integration_closure.py` — closure E2E
