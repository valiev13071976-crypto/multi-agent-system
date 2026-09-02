# Controlled Automation Expansion

Bounded policy-governed business automations extending Scheduled Business Automation.

## Architecture

```
User / BA / API
    → ControlledAutomationDefinition
    → Trigger (TIME/SCHEDULE/EVENT/MANUAL)
    → Condition Evaluation (declarative, bounded)
    → Policy Envelope + Risk Class
    → Decision (auto / PREPARED / WAITING_APPROVAL / BLOCKED)
    → Existing Scheduled/Durable Execution
    → Existing Workflow / ToolGateway / Integrations
    → Verification / Audit / Analytics
```

Automation does **not** grant itself authority. Permissions are checked at execution time.

## Package

`controlled_automation/` — models, conditions, policy, risk, events, service, store, dispatcher, router, runtime.

Reuses: `scheduled_automation`, workflow queue, BA HITL patterns, tenant/RBAC, idempotency semantics.

## Triggers

| Type | Semantics |
|------|-----------|
| TIME / SCHEDULE | Via existing scheduler (no duplicate timezone engine) |
| BUSINESS_EVENT | Versioned envelope with idempotency |
| MANUAL | Governed run-now |
| CONDITION_CHECK | Periodic evaluation |

## Conditions

Declarative operators: EQ, NE, GT, GTE, LT, LTE, IN, NOT_IN, EXISTS, CHANGED, PERCENT_CHANGE_GT/LT.

Logical: ALL / ANY with bounded depth.

Data quality: KNOWN, UNKNOWN, STALE, PARTIAL, ERROR — unknown/stale/partial fail closed for writes.

## Policy Envelope

- Action/integration/resource allowlists
- max_actions_per_run/hour/day
- requires_approval, allow_auto_execute, dry_run
- cooldown, valid_from/until
- kill switch scopes: GLOBAL, TENANT, AUTOMATION, INTEGRATION, ACTION_TYPE

## Risk Classes

| Class | Default behavior |
|-------|------------------|
| R0_READ_ONLY | Auto-run allowed |
| R1_PREPARE_ONLY | Auto-run to PREPARED |
| R2_REVERSIBLE_LOW_RISK_WRITE | Auto only if policy allows |
| R3/R4 | HITL required |

## Dry Run

Same policy path as execution; zero external mutations.

## Flags

```
CONTROLLED_AUTOMATION_EXPANSION_ENGINEERING_READY=true
CONTROLLED_AUTOMATION_EXPANSION_LIVE_ACTIVE=false
CONTROLLED_AUTOMATION_EXPANSION_LIVE_VERIFIED=false
```

## Non-goals

Full autonomy, arbitrary code execution, parallel scheduler/workflow core, production LIVE activation.
