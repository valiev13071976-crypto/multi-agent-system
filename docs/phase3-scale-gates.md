# Phase 3 — Scale Gates & Data Plane Decision Matrix

Benchmark-derived **starting thresholds**, not production SLAs.

## CORRECTNESS VALIDATED

Covered by unit/integration + Block 1/2/3 regressions:

- API/Worker roles, durable queue claim/lease/fencing
- Scheduler window claim + crash recovery
- Admission / pending / running limits
- Lanes, interactive reservation, fairness, aging
- Shared budget + ProviderGovernor + breaker + router capacity fallback

## SYNTHETIC LOAD VALIDATED

`python -m harness.load_harness --scenario all`

Scenarios: interactive baseline, background saturation, tenant bully, schedule storm, worker loss, provider saturation, 429 storm, sqlite contention.

## REAL PRODUCTION CAPACITY

**Unknown** until measured on target hardware and real provider quotas.

Do **not** claim “supports N users” from unit/harness numbers.

---

## Operational starting thresholds (single-host SQLite)

| Signal | Watch | Investigate / gate |
|--------|-------|--------------------|
| interactive oldest queue age | > 5s | > 30s sustained |
| claim latency p95 (harness) | > 50ms | > 200ms sustained |
| workflow write latency p95 | > 50ms | > 200ms |
| sqlite_busy_count growth | rising with workers | busy rate dominates throughput |
| budget reservation latency | > 50ms | > 200ms |
| governor acquire latency | > 20ms | > 100ms |
| interactive admission reject rate | > 1% | > 5% sustained |

These are **starting ops thresholds**, not SLAs.

---

## SQLite store matrix

| Store | Shared file | Verdict | Evidence |
|-------|-------------|---------|----------|
| workflow state | SIDE_EFFECT_DB_PATH | **SAFE FOR CURRENT STAGE** | Block 1/2 multi-process tests; CAS/row_version |
| checkpoints | same | **SAFE FOR CURRENT STAGE** | recovery tests |
| queue_tasks | same | **SAFE FOR CURRENT STAGE** / **WATCH** under high writer count | atomic claim; contention harness |
| schedules | same | **SAFE FOR CURRENT STAGE** | window claim + crash recovery |
| side-effect ledger | same | **SAFE FOR CURRENT STAGE** | idempotency/reconciliation suite |
| budget | same path (separate opener) | **SAFE FOR CURRENT STAGE** / **WATCH** | BEGIN IMMEDIATE reserve races pass |
| provider governor | same path (separate opener) | **SAFE FOR CURRENT STAGE** / **WATCH** | shared concurrency tests |

**MIGRATION REQUIRED**: none for single-host multi-process correctness today.

---

## PostgreSQL decision

```text
POSTGRES NOT REQUIRED YET
```

**Triggers to reopen:**

- Sustained sqlite_busy with claim/write latency degradation
- Adding workers no longer increases throughput (writer serialization wall)
- Multi-host deployment required
- HA / managed backup-restore requirements beyond file copy
- Operational need for concurrent writers across hosts

**Before** multi-host production: migrate durable data plane to network DB (Postgres).

---

## Redis decision

```text
REDIS NOT REQUIRED YET
```

Queue, governor, budget, and leases are durable SQLite contracts. Redis is optional later for:

- ephemeral cache
- edge rate-limit if API fan-out exceeds SQLite governor usefulness

Not required solely because the system is multi-worker.

---

## Multi-host boundary

| Stage | Data plane | Status |
|-------|------------|--------|
| single host + multiple processes | shared SQLite file + WAL | **supported** |
| multiple hosts | shared SQLite file | **not a normal data plane** |
| multiple hosts | Postgres (or equivalent) | **required for that stage** |

---

## Routing health / runtime-stats scope (PATCH-MR-05)

| Signal | Current value | Notes |
|--------|---------------|-------|
| `routing_health_scope` | `process_local` | `ProviderHealthTracker` in-memory per process |
| `routing_runtime_stats_scope` | `process_local` | `ProviderRuntimeStatsAggregator` in-memory per process |
| `routing_health_shared_backing` | `not_available` | No Redis/DB sync implemented |
| `multi_worker_shared_routing_health_ready` | `false` | Cross-worker cooldown sync not guaranteed |

**CURRENT:** cooldown and runtime-stat samples are process-local.

**SAFE:** single-process / `combined`, or multi-worker deployments that **accept** per-worker health isolation (Worker A cooldown does not propagate to Worker B).

**NOT YET GUARANTEED:** cross-worker cooldown or runtime-stat synchronization.

**FUTURE:** shared backing store during Scale/HA work (pluggable via `agents.routing_state_scope` protocols). Do **not** claim distributed routing health exists today.

Machine-visible surfaces: `/ready` → `capabilities`, dependency details `routing_provider_health` / `routing_runtime_stats`; `/metrics/runtime` → `routing_coordination`.

Liveness stays healthy when shared routing health is absent. Ordinary single-process readiness is not failed solely for process-local scope.

---

## Deployment topology

### DEVELOPMENT

```text
COMBINED + SQLite + WORKER_LANES=all
PANDA_RUNTIME_PROFILE=development
```

### INITIAL PRODUCTION (single host)

```text
API (RUNTIME_ROLE=api)
Interactive Worker (worker + WORKER_LANES=interactive)
Background Worker (worker + background,bulk,scheduled)
shared local SIDE_EFFECT_DB_PATH
PANDA_RUNTIME_PROFILE=multi-process-production
```

### SCALED PRODUCTION (multi-host)

```text
multiple API replicas
multiple lane workers
shared network DB (Postgres)  ← gate, not implemented in Block 3
```

No Kubernetes in this phase.

---

## Retention / cleanup (P1 unless proven blocker)

| Data | Policy |
|------|--------|
| completed queue tasks | can expire/compact (P1) |
| dead-lettered tasks | retain for ops window |
| workflows / checkpoints | retain per audit needs |
| schedules | retain while enabled |
| SE ledger / idempotency | **must retain** for correctness |
| reconciliations | retain |
| budget ledger | retain for FinOps |
| governor slots | TTL expire (already) |
| governor state | retain / soft reset on success |

Block 3 does **not** ship archival; no correctness blocker observed that requires cleanup now.
