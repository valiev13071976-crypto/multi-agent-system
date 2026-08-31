# Data Acquisition & Parsing Platform

## Architecture

One shared acquisition pipeline for search, SEO, marketplace intelligence, Content Factory, and future modules:

```
SOURCE
  ↓
AcquisitionRequest / AcquisitionJob
  ↓
Tool Platform (ToolGateway → http.request / search / browser)
  ↓
RawArtifact (snapshot)
  ↓
ParserRegistry → ParsedRecord
  ↓
RecordNormalizer → cleaning
  ↓
DedupeEngine → change detection
  ↓
IngestionTarget → DatasetResult
  ↓
Downstream consumers
```

**No parallel crawler cores.** Crawl/scrape jobs use `ControlledCrawler` / `ScrapePipeline` through `AcquisitionManager`, which is the only network entry point.

## Core contracts

| Concept | Module |
|---------|--------|
| Acquisition request | `acquisition/models.py` — `AcquisitionRequest` |
| Source definition | `SourceDefinition`, `SourceDescriptor` |
| Crawl policy | `CrawlPolicy` (bounded depth/pages/frontier/retries) |
| Raw snapshot | `RawArtifact` |
| Parsed record | `ParsedRecord` |
| Job / frontier | `AcquisitionJob`, `FrontierEntry` |
| Source categories | `acquisition/source_categories.py` — `WEB_URL`, `WEB_SITE`, … |
| Errors | `acquisition/errors.py` |

## Tool Platform integration

```
Workflow / Agent
      ↓
Tool Router → Tool Registry → Tool Gateway
      ↓
HttpAdapter / Search / Browser contract adapters
      ↓
AcquisitionManager.acquire()
```

Side effects, tenant isolation, capabilities, and SSRF checks remain in the closed Tool Platform.

## Safe network policy

- Central URL validation: `tools/url_safety.py`
- Source host allowlists: trusted control-plane only (`SourceDefinition.allowed_hosts`)
- Payload cannot widen hosts or override tenant
- Redirect validation: `validate_redirect()` (manual redirect loops must revalidate)
- Robots: `acquisition/robots.py` — centralized cache + fail-closed when rules unavailable

## Crawler / frontier

`ControlledCrawler` provides:

- Bounded depth, pages, frontier, bytes, retries, deadline
- Per-host politeness and 429 backoff
- Durable frontier + checkpoint/resume (SQLite store)
- Domain scope (same-host allowlist)
- Cancel support
- Batch workload stamping via `AcquisitionPlanner`

## Parsing / normalization

- `ParserRegistry` — content-type routing (HTML, JSON, XML, CSV)
- Domain parsers (supplier, marketplace, competitor, search) are plugins
- `RecordNormalizer` — deterministic normalization (no invented values)
- `DedupeEngine` — URL, raw hash, fingerprint, composite key layers
- `detect_record_change` — NEW / UNCHANGED / CHANGED / REMOVED

## Trust boundary

All acquired content is **untrusted external data** (`CONTENT_TRUST_UNTRUSTED`). Scraped text must never alter platform permissions or be treated as system instructions.

## How to add a new acquisition source

1. Define `SourceDefinition` with `allowed_hosts`, `crawl_policy`, trust level, and `tool_id` (`http.request` or `search`).
2. Register via `AcquisitionService.register_source_definition()`.
3. Optionally set `metadata["source_category"]` using tokens from `source_categories.py`.
4. Plan job via `AcquisitionService.plan_job()` — large crawls auto-stamp batch/bulk lane.
5. Run via `run_crawl_job()` / `run_scrape_job()` / `acquire()`.
6. Add parser if new record shape needed (`ParserRegistry.register`).

## How to add a new parser

1. Implement parser class with `parser_id`, `parser_version`, `source_types`, and `parse(artifact) -> ParsedRecord`.
2. Register in `build_default_parser_registry()` or at runtime.
3. Add contract tests using deterministic fixtures (no live internet).

## Storage boundaries

| Layer | Store |
|-------|-------|
| Raw snapshots | `AcquisitionStore` / `SqliteAcquisitionStore` |
| Parsed records | Same store + ingestion target |
| Job/frontier state | SQLite durable schema v2 |
| Tool artifacts | `tools/artifacts.py` (separate, tenant-scoped) |

## Downstream readiness

Generic outputs exposed for later modules:

- Canonical URL, title, headings, links, structured data, content hash, change history, provenance
- No SEO scoring, marketplace business logic, or content generation in this platform

## Tests

- `tests/test_acquisition_platform.py` — core contracts
- `tests/test_acquisition_scale_platform.py` — scale, SSRF, batch, crawler, dedupe, E2E
- `tests/test_acquisition_persistence.py` — durable restart + tenant isolation
- `tests/test_acquisition_expansion_closure.py` — applied expansion closure

See also `docs/acquisition-bypass-audit.md` for forbidden bypass paths.
