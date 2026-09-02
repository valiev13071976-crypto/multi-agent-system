# Scheduled Business Automation

Governed time-based scheduling layer on top of the existing Panda platform. The scheduler answers **when** to attempt governed work; authorization, HITL, idempotency, and FinOps remain in existing execution paths.

## Architecture

```
User / BA / API
    → ScheduleDefinition (store)
    → ScheduledAutomationService.tick()
    → claim occurrence + misfire policy
    → ScheduledAutomationDispatcher
    → existing workflow queue (execution_lane=scheduled)
    → Business Assistant / ToolGateway / integrations
```

**Not in scope:** parallel workflow engine, cron daemon, direct provider calls, arbitrary code execution.

## Package

`scheduled_automation/` — models, recurrence, store, service, dispatcher, router, runtime, observability.

Reuses:

- `workflow` durable queue via `build_scheduled_automation_runtime(workflow_runtime=...)`
- `security.identity` / RBAC for tenant isolation
- `BusinessAssistantService` steps `schedule_intent` and `schedule_create`

## Schedule types

| Type | Semantics |
|------|-----------|
| `ONCE` | Single run at `start_at` |
| `INTERVAL` | Every `interval_seconds` (minimum 60s) |
| `DAILY` | Wall-clock `daily_time` in `timezone` |
| `WEEKLY` | `weekly_day` (0=Mon) + `daily_time` in `timezone` |

Timestamps stored in UTC; wall-clock recurrence uses explicit `zoneinfo` timezone (DST-aware).

## Misfire policy

| Policy | Behavior |
|--------|----------|
| `SKIP` | Missed occurrence skipped |
| `RUN_ONCE` | One catch-up at current time |
| `CATCH_UP_BOUNDED` | Bounded catch-up (`MAX_CATCH_UP_OCCURRENCES=3`) |

## Overlap

Default `FORBID`: new tick skipped while prior occurrence still marked running.

## Occurrence identity

- `occurrence_id`: `{schedule_id}:v{version}:{scheduled_for_utc_iso}`
- `execution_key`: `schedule-occurrence:{schedule_id}:{version}:{unix_ts}`

At-least-once dispatch + idempotent business execution (no exactly-once delivery claim).

## Target types (allowlist)

- `WORKFLOW`
- `BUSINESS_ASSISTANT_REQUEST`
- `ANALYTICS_QUERY`
- `INTEGRATION_READ`

Forbidden payload keys: `code`, `script`, `shell`, `eval`, `sql`, `exec`.

## API

Base: `/api/v1/automations`

| Method | Path |
|--------|------|
| POST | `/schedules` |
| GET | `/schedules` |
| GET | `/schedules/{id}` |
| PATCH | `/schedules/{id}` |
| POST | `/schedules/{id}/enable` |
| POST | `/schedules/{id}/disable` |
| POST | `/schedules/{id}/pause` |
| POST | `/schedules/{id}/resume` |
| POST | `/schedules/{id}/run-now` |
| GET | `/schedules/{id}/runs` |
| GET | `/status` |

All endpoints: auth required, tenant-scoped, RBAC via `ScheduleAccessPolicy`.

## Permissions

- `schedule.read`
- `schedule.create`
- `schedule.update`
- `schedule.enable`
- `schedule.run_now`

Capabilities in schedule definition are re-checked at execution time (not eternal snapshot).

## HITL

If `target_payload.requires_approval` is true, occurrence moves to `WAITING_APPROVAL` without side effects.

## Fixture / live boundary

| Flag | Default |
|------|---------|
| `SCHEDULED_BUSINESS_AUTOMATION_ENGINEERING_READY` | `true` |
| `SCHEDULED_BUSINESS_AUTOMATION_LIVE_ACTIVE` | `false` |
| `SCHEDULED_BUSINESS_AUTOMATION_LIVE_VERIFIED` | `false` |

`SCHEDULED_AUTOMATION_MODE=FIXTURE` (default). LIVE requires `SCHEDULED_AUTOMATION_LIVE_ENABLED` and fails closed until production activation is implemented.

## Recovery

Definitions and occurrences persist in `SqliteScheduleAutomationStore` (or in-memory for tests). After restart, `next_run_at` and due schedules are reloaded; claim/lease prevents duplicate dispatch.

## Non-goals

Event-driven automation, external webhooks, arbitrary cron shell, production deployment, live provider activation in this block.
