# Production Business E2E

Cross-module end-to-end validation that Panda behaves as **one governed business platform** across already-closed modules.

## Purpose

Prove that a production-shaped request can traverse:

```
API / Business Assistant
→ planning & orchestration
→ tenant / RBAC / capability
→ workflow & durable queue
→ ToolGateway / Integration Activation
→ business modules (Excel, marketplace, CRM, analytics, …)
→ HITL / idempotency / FinOps / audit
→ deterministic fixture external boundary
```

## Production-shaped ≠ production-live

| In scope | Out of scope |
|----------|--------------|
| Real internal services & governance paths | Real email send |
| Fixture/sandbox external adapters | Live marketplace writes |
| Tenant isolation, HITL, idempotency semantics | Bitrix/1C production mutations |
| Deterministic evidence model | Production deployment |

## Flags

```
PRODUCTION_BUSINESS_E2E_ENGINEERING_READY=true
PRODUCTION_BUSINESS_E2E_LIVE_ACTIVE=false
PRODUCTION_BUSINESS_E2E_LIVE_VERIFIED=false
```

## Package

`production_business_e2e/` — test/evidence harness only (not a parallel runtime):

| Module | Role |
|--------|------|
| `harness.py` | `build_e2e_world()` — composed BA + integrations + analytics + scheduling |
| `fixtures.py` | Tenant activation, supplier seed, API auth env |
| `scenarios.py` | Canonical scenario runners returning `E2EEvidence` |
| `runner.py` | `run_scenario()` / `run_canonical_suite()` |
| `models.py` | Structured evidence schema |
| `evidence.py` | Secret checks, summaries |

## Canonical scenarios

| ID | Scenario |
|----|----------|
| A | Supplier price list → Excel/Data → analysis → enrichment handoffs |
| B | Product → marketplace economics (margin, loss, unknown costs) |
| C | Product enrichment (content / SEO / media handoffs) |
| D | Selective marketplace preparation (not full catalog) |
| E | Governed marketplace write (approval → one fixture effect → idempotency) |
| F | Bitrix publication (HITL → resume) |
| G | 1C stock read / reconciliation shape |
| H–J | CRM ↔ email ↔ calendar cross-system prep |
| K | Analytics business question (governed read model) |
| L | Scheduled automation → dispatch → durable queue metadata |
| M–N | Large Excel batch lane; crawler URL policy |
| O | Uncertain write → reconcile (no blind retry) |

## Governance invariants verified

- Cross-tenant denial (integration, BA, analytics, schedules)
- HITL before external writes; rejected approval → zero effects
- Idempotent gateway writes & duplicate dispatch protection
- Revoked capability blocks scheduled occurrences
- Prompt injection in untrusted content sanitized
- Secrets not exposed in API/evidence responses
- Decimal-safe money; NO_DATA ≠ zero; unknown cost ≠ zero

## Evidence model

Each scenario returns machine-readable `E2EEvidence`:

- `scenario_id`, `tenant_id`, `status`, timestamps
- workflow/execution/schedule references
- steps, business_result, side_effects, approvals, audit_refs
- `fixture_mode=true`, `live_active=false`

## Tests

Dedicated suite: `tests/test_production_business_e2e_closure.py`

## Non-goals

- Re-auditing individual CLOSED platform blocks
- New workflow engine, scheduler, or BA core
- Production activation or Railway changes
