# Acquisition Platform — Bypass Audit

Legitimate low-level paths vs forbidden Agent→network shortcuts for the
Data Acquisition & Parsing Platform (5.1–5.7).

## Governed path (required)

```
Workflow → AcquisitionJob → Source Policy → Tool Platform → Production Runtime
  → fetch → raw artifact → parse → normalize → dedupe → ingest → dataset
```

Large crawl/scrape jobs stamp **trusted** TaskQueue metadata
(`trusted_job_type=crawler|scraping`, `workload_class=batch`,
`execution_lane=bulk`) so they **cannot** land on the interactive pool
even if a caller omits or forges a workload hint.

## Legitimate paths

| Path | Why it is not a bypass |
|------|------------------------|
| `AcquisitionManager` → `ToolGateway.invoke` | Sole network entry; SSRF via `tools.url_safety` |
| `ControlledCrawler` / `ScrapePipeline` injectable `fetch_fn` | Test-only Fake HTTP; production uses manager |
| `SecretReference` on `SourceDefinition.auth_secret_ref` | Opaque ref only — no plaintext secrets in jobs |
| Durable SQLite artifact refs (`raw_artifact_ref`) | Ownership bound to tenant/job/source/stage |
| `classify_workload` + stamped metadata | Trusted keys only; payload cannot widen hosts |

## Not allowed

- Agent → `httpx` / `requests` / browser drivers directly
- Parallel crawler queue outside TaskQueue bulk lane
- Separate tenant or artifact stores outside acquisition + Tool Platform
- Payload override of `allowed_hosts` / tenant / path policy
- Live Internet in unit tests (Fake HTTP only)

## Codebase scan notes

- `acquisition/crawler.py` and `acquisition/scrape/pipeline.py` fetch only via
  `AcquisitionManager` or injected test doubles.
- Browser **write** remains out of scope; browser read may use gateway scaffold.
- No Redis / K8s crawler cluster — frontier is SQLite-backed for production jobs.
