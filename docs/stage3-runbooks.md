# Stage 3 — Operational Runbooks

## Provider outage

**Trigger:** Provider health degraded/circuit open; elevated error rate on AI/Telegram/email/billing.

**Inspect:** Block-15 `/api/admin/ops/providers`, `/api/admin/ops/production-integrations`, routing health.

**Safe action:** Confirm circuit state; wait for cooldown; verify fallback routing policy; disable affected provider via config if needed.

**Do NOT:** Paste API keys into tickets; bypass Model Router; retry auth failures indefinitely.

**Recovery verification:** Provider health returns healthy; smoke test passes; error rate normalizes.

**Escalate if:** All launch-critical AI providers unavailable >15 minutes.

## Queue backlog

**Trigger:** Queue depth alert; tasks pending > threshold.

**Inspect:** Block-15 queue view; worker pool drain state; lane distribution.

**Safe action:** Scale workers if platform supports; drain misconfigured lane; redrive DLQ after root-cause fix.

**Do NOT:** Delete queue rows manually; run unbounded replays.

## DLQ growth

**Trigger:** DLQ depth increasing.

**Inspect:** DLQ entries in Block-15; normalized failure category; tenant scope.

**Safe action:** Fix underlying provider/config issue; governed redrive with idempotency keys.

## Worker crash

**Trigger:** Stale worker leases; tasks stuck in claimed state.

**Inspect:** Worker health; lease expiry; task reclaim metrics.

**Safe action:** Restart worker process; verify reclaim and single-owner fencing.

## Database unavailable

**Trigger:** Readiness dependency DB failure; persistence errors.

**Inspect:** `/ready` dependencies; SQLite path under `PANDA_DATA_DIR`; disk space.

**Safe action:** Restore from last good backup to isolated target if corruption suspected.

**Do NOT:** Delete production SQLite files while traffic active.

## Storage unavailable

**Trigger:** Artifact write failures; backup failures; disk threshold alert.

**Inspect:** `PANDA_DATA_DIR` mount; artifact root permissions; backup manifest freshness.

## Backup failed / stale

**Trigger:** Backup alert; missing manifest; stale backup age.

**Safe action:** Run `python -m production_foundation.cli backup`; verify manifest checksums; configure off-host destination.

## Failed deployment

**Trigger:** Railway deploy unhealthy; readiness FAIL.

**Safe action:** Inspect build logs; verify env vars; rollback to previous release.

## Rollback

**Trigger:** Post-deploy smoke/regression failure.

**Safe action:** Roll back Railway service to prior deployment; run smoke suite; verify synthetic durable state intact.

## Secret rotation

**Trigger:** Scheduled rotation or suspected compromise.

**Safe action:** Update Railway env secret; restart service; verify provider health; revoke old credential at provider.

## Suspected tenant/security incident

**Trigger:** Cross-tenant access report; credential leak suspicion.

**Safe action:** Rotate affected secrets; audit Block-15 access logs; isolate tenant; run security probes.

**Do NOT:** Export raw customer data into tickets.
