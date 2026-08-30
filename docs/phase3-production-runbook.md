# Phase 3 — Production Runbook

Operational procedures for the SQLite-first multi-process runtime.
Commands assume repo root and a configured `.env`.

## Profiles

```bash
# development (default)
set PANDA_RUNTIME_PROFILE=development

# single-node production (COMBINED or split on one host)
set PANDA_RUNTIME_PROFILE=single-node-production

# multi-process (set RUNTIME_ROLE per process)
set PANDA_RUNTIME_PROFILE=multi-process-production
set RUNTIME_ROLE=api          # API process
set RUNTIME_ROLE=worker
set WORKER_LANES=interactive  # interactive worker
set WORKER_LANES=background,bulk,scheduled
```

Validate config (fail-fast):

```bash
python -c "from config.runtime_config import validate_runtime_config; print(validate_runtime_config())"
```

## START

```bash
# Combined local
set RUNTIME_ROLE=combined
uvicorn main:app --host 0.0.0.0 --port 8000

# Split
# Terminal A
set RUNTIME_ROLE=api
uvicorn main:app --host 0.0.0.0 --port 8000
# Terminal B
set RUNTIME_ROLE=worker
set WORKER_LANES=interactive
uvicorn main:app --host 0.0.0.0 --port 8001
# Terminal C
set RUNTIME_ROLE=worker
set WORKER_LANES=background,bulk,scheduled
uvicorn main:app --host 0.0.0.0 --port 8002
```

Workers share `SIDE_EFFECT_DB_PATH` (same host / same file).

## STOP / DRAIN

### API drain

```http
POST /admin/drain
```

Sets readiness=`not_ready`, stops new expensive admission (429 on workflows while draining).

Or:

```bash
curl -X POST http://localhost:8000/admin/drain
```

Then stop the process (Ctrl+C / systemd stop). Lifespan calls `stop_background()`.

### Worker drain

```http
POST /admin/drain?wait_seconds=5
```

Stops new claims + schedule ticks, bounded wait, then exits background loop. Running work recovers via lease expiry on other workers.

## API RESTART

1. `POST /admin/drain` on the API replica
2. Stop process
3. Start API with `RUNTIME_ROLE=api`
4. Confirm `GET /ready` → readiness healthy/degraded (not draining)

Startup order vs workers does **not** matter for correctness.

## WORKER RESTART

1. Drain worker (`POST /admin/drain`)
2. Stop process
3. Start worker (`RUNTIME_ROLE=worker`, lanes set)
4. Worker runs recovery + reclaim of expired leases only

## QUEUE BACKLOG

```http
GET /metrics/runtime
```

Check `pending_global`, `queue_depth_by_lane`, `interactive_slo.interactive_oldest_queue_age`.

Actions:

- Scale background workers (`WORKER_LANES=background,bulk,scheduled`)
- Tighten `MAX_PENDING_*` / admission
- Do **not** raise interactive reservation without capacity headroom

## INTERACTIVE LATENCY SPIKE

Inspect:

- `interactive_slo.*` on `/metrics/runtime`
- `running_by_lane` / `queue_depth_by_lane.interactive`
- `sqlite_busy_count`, `capacity_throttle_count`

Actions:

- Ensure interactive workers exist
- Confirm `INTERACTIVE_RESERVED` not consumed by borrow when interactive pending
- Reduce background concurrency / pause bulk admission

## PROVIDER 429

Shared governor records Retry-After throttle.

- Check `provider_429`, `provider_breaker_state` on metrics
- Wait for throttle/cooldown; do not restart-loop workers to “retry faster”
- Router falls back when capacity/breaker OPEN

## PROVIDER BREAKER OPEN

- Qualifying failures trip breaker → OPEN → HALF_OPEN probe → CLOSED
- Non-qualifying (validation/capability) do not open breaker
- Metrics: `provider_breaker_state`

## ROUTING HEALTH SCOPE (process-local)

Provider auto-routing cooldown (`ProviderHealthTracker`) and runtime stats are
**process-local**. They are **not** shared across workers.

Operators:

- Inspect `/ready` → `capabilities.routing_health_scope` (`process_local`)
- Inspect `capabilities.routing_health_shared_backing` (`not_available`)
- Inspect `/metrics/runtime` → `routing_coordination`

**SAFE:** single-process, or multi-worker accepting independent per-worker cooldown.

**NOT YET GUARANTEED:** Worker A marking provider X unhealthy does not update Worker B.

Do not restart-loop workers expecting global cooldown sync. See `docs/phase3-scale-gates.md`.

## SQLITE CONTENTION

Signals: rising `sqlite_busy_count`, claim latency p95 on harness / metrics.

```bash
python -m harness.load_harness --scenario sqlite_contention --json
```

If sustained busy + latency growth under more workers → see `docs/phase3-scale-gates.md` (Postgres decision).

## BUDGET REJECTION SPIKE

- Shared `SqliteBudgetStore` on `SIDE_EFFECT_DB_PATH` / `FINOPS_BUDGET_DB_PATH`
- Check FinOps policies / daily limits
- Metrics: `budget_rejections`

## SCHEDULE RECOVERY

Schedules use claim-before-enqueue; stale claims re-fire with deterministic execution keys.

- Workers (not API) tick schedules
- Duplicate logical windows prevented by claim CAS + execution_key

## BACKUP

Consistent SQLite backup of the durable data plane file(s):

Primary (default one file):

- `SIDE_EFFECT_DB_PATH` (workflows, queue, schedules, SE ledger, governor tables)

Also backup if overridden separately:

- `FINOPS_BUDGET_DB_PATH`
- `PROVIDER_GOVERNOR_DB_PATH`

Suggested (process quiet / drained):

```bash
# After API+worker drain
python -c "import shutil,os; shutil.copy2(os.environ.get('SIDE_EFFECT_DB_PATH','./data/side_effects.sqlite3'), './backups/side_effects.backup.sqlite3')"
```

Prefer SQLite online backup API under load; copy2 is smoke-level when drained.

## RESTORE

1. Stop all API/worker processes
2. Replace DB file(s) from backup
3. Start workers then API (order optional)
4. `GET /ready`
5. Smoke: list workflow status / queue metrics; confirm no unexpected duplicate active leases

See `tests/test_phase3_block3_hardening.py` backup smoke test.
