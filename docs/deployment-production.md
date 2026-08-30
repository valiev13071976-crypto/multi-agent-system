# Block Stage 1 — Production Foundation

## Railway deployment

| Setting | Value |
|---------|-------|
| Builder | RAILPACK (`railway.toml`) |
| Start command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Health check | `GET /health` |
| Readiness | `GET /ready` |

## Required environment variables (production)

| Variable | Classification | Description |
|----------|----------------|-------------|
| `PANDA_ENV` | REQUIRED | Must be `production` |
| `PANDA_DATA_DIR` | REQUIRED | Persistent volume mount path (e.g. `/data`) |
| `SIDE_EFFECT_PERSISTENCE_BACKEND` | REQUIRED | Must be `sqlite` |
| `SIDE_EFFECT_DB_PATH` | DERIVED | Defaults to `$PANDA_DATA_DIR/side_effects.sqlite3` |
| `SAAS_PRODUCT_DB_PATH` | DERIVED | Defaults to `$PANDA_DATA_DIR/saas_product.sqlite` |
| `PANDA_ARTIFACT_ROOT` | DERIVED | Defaults to `$PANDA_DATA_DIR/artifacts` |
| `PANDA_BACKUP_ROOT` | DERIVED | Defaults to `$PANDA_DATA_DIR/backups` |
| `PUBLIC_URL` | REQUIRED | Trusted public origin (HTTPS) |
| `SECURITY_AUTH_MODE` | REQUIRED | Must be `required` |
| `PANDA_API_KEYS` | SECRET | API key credentials |
| `SECURITY_CORS_ORIGINS` | REQUIRED | Explicit comma-separated origins (no `*`) |

## Optional variables

| Variable | Description |
|----------|-------------|
| `PANDA_BACKUP_DESTINATION` | `local` (default) or external destination key |
| `PANDA_ALERT_WEBHOOK_URL` | External alert webhook (Stage 2 operator wiring) |
| `PANDA_BACKUP_STALE_HOURS` | Backup freshness alert threshold (default 26) |
| `PANDA_DISK_FREE_THRESHOLD_BYTES` | Low disk alert threshold (default 100MB) |
| `RUNTIME_ROLE` | `combined` (default single-node Railway) |

## Persistent volume (OPERATOR_ACTION_REQUIRED)

Mount a Railway persistent volume at `/data` and set:

```
PANDA_DATA_DIR=/data
```

## Backup

Run via API/admin or scheduled operator job:

```bash
python -m production_foundation.cli backup
```

Backups use SQLite online backup API + artifact directory copy with manifest/checksum.

**Off-host backup destination is OPERATOR_ACTION_REQUIRED before serious live traffic.**

## Restore (isolated test/recovery)

```bash
python -m production_foundation.cli restore --backup-id <id> --target-dir /recovery/data
```

Never restore directly over live production without stop/drain procedure.

## Recovery runbook

1. Stop/drain traffic (`POST /internal/drain` if exposed)
2. Identify last good backup manifest under `$PANDA_BACKUP_ROOT/<backup_id>/`
3. Verify manifest checksums
4. Restore to isolated directory
5. Validate tenant/subscription/workflow samples
6. Promote/switch data directory or redeploy with restored volume snapshot
7. Confirm `/ready` PASS and run smoke tests
8. Monitor alerts for 15–30 minutes

## RPO / RTO

- **RPO:** Depends on backup schedule (default target: ≤24h with daily backup)
- **RTO:** Manual restore procedure; not automated DR

## Rollback

Application rollback is safe when schema is backward compatible. If migration is forward-only, rollback requires restore from pre-deploy backup.

## Operator verification checklist

- [ ] Railway volume mounted at `/data`
- [ ] All required env vars set in Railway dashboard
- [ ] Custom domain DNS → Railway (OPERATOR_ACTION_REQUIRED)
- [ ] TLS certificate active (Railway-managed)
- [ ] `/health` returns 200
- [ ] `/ready` returns 200 after deploy
- [ ] `/api/admin/ops/production-foundation` shows PASS (platform admin)
- [ ] External backup destination configured
- [ ] External alert webhook configured (optional)
