# Block 16 — Commercial SaaS Deployment

## Required environment variables

| Variable | Required (prod) | Description |
|----------|-----------------|-------------|
| `PANDA_ENV` | yes | `production` enables fail-closed checks |
| `SECURITY_AUTH_MODE` | yes | Must be `required` in production |
| `PANDA_API_KEYS` | yes | API key credentials |
| `SAAS_PRODUCT_DB_PATH` | yes | Persistent SQLite path for SaaS state |
| `SAAS_BILLING_ENABLED` | optional | Default `true` |
| `SAAS_BILLING_PROVIDER` | optional | `fake` for test; live provider P2 |
| `PUBLIC_URL` | recommended | Public application URL |
| `SECURITY_CORS_ORIGINS` | recommended | Explicit comma-separated origins (no `*`) |

## Fake billing safety

Production with `SAAS_BILLING_ENABLED=true` and `SAAS_BILLING_PROVIDER=fake` **fails startup/readiness**.

Use `SAAS_BILLING_ENABLED=false` or a live provider in production.

## Migrations

SaaS schema is applied automatically on startup via `SqliteSaaSProductStore` (`schema_version=1`).

## Health / readiness

- `/health` — liveness
- `/ready` — runtime readiness
- `/api/product/readiness` — commercial configuration report

## Smoke test

```bash
pytest tests/test_saas_product_block16_platform.py -q
```

## Backup

Back up `SAAS_PRODUCT_DB_PATH` SQLite file and side-effect persistence DB together for consistent tenant/commercial state.
