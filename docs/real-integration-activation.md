# Real Integration Activation

## Purpose

Governed activation layer that turns logical Panda capabilities into **verified, tenant-scoped, environment-aware** external connections.

```
Business Assistant / Workflows
        ↓
ToolGateway (unchanged)
        ↓
Integration Activation Layer
        ↓
Provider Adapter (native or Composio)
        ↓
External Service
```

## Boundaries

- **NOT** a new Tool Platform / Workflow Engine / Business Assistant core
- Reuses `integrations/` secrets, registry patterns, circuit breaker, ledger, health
- Reuses HITL / approval / idempotency / FinOps / tenant isolation

## Provider vs Connection

| Concept | Meaning |
|---------|---------|
| Provider | Bitrix, Ozon, Composio, … (definition) |
| Connection | Tenant-specific configured instance |

## Environments

`FIXTURE` | `SANDBOX` | `LIVE`

- LIVE never falls back to SANDBOX/FIXTURE
- Fixture adapters are explicitly `live=false`

## Lifecycle

`UNCONFIGURED → CONFIGURED → VERIFYING → ACTIVE`

Failure/control: `DEGRADED | FAILED | REVOKED | DISABLED`

**ACTIVE requires successful non-destructive verification.**

## Secrets

Only `secret:…` credential refs. Plaintext tokens rejected. Never in evidence/logs/connection public status.

## Capability resolution

`resolve_connection(tenant, capability, environment, operation_class)`

Deterministic (priority, connection_id). Explicit connection id tenant-validated.

READ vs WRITE are separate. Auth ≠ write permission.

## Composio

OPTIONAL broker adapter. **Not** the Panda Tool Platform.

Platform key ≠ user Gmail connection (`USER_CONNECTION_REQUIRED`).

Unknown discovered tools denied by default. Writes still require governance/approval.

## Native readiness

Provider catalog includes: Bitrix/Aspro, 1C, WB, Ozon, Yandex Market, Email, Calendar, CRM, Analytics.

## Principle

> A real credential gives CONNECTIVITY.  
> It does **not** automatically give AUTHORIZATION.

## Tests

`tests/test_real_integration_activation_closure.py`
