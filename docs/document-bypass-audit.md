# Document Platform — Bypass Audit (Block 6)

Legitimate low-level paths vs forbidden Agent→document shortcuts for
Files & Document Intelligence (6.1–6.7).

## Governed path (required)

```
Workflow / ToolGateway → DocumentService / DocumentIntelligenceService
  → type_detect → parsers (pdf/docx/…) → store (tenant-scoped)
  → OCR plan → classify → extract → validate → compare / reconcile / generate
```

Large OCR / large PDF / bulk compare-reconcile-generate jobs stamp **trusted**
TaskQueue metadata (`trusted_job_type=document_ocr|document_large|document_bulk`,
`workload_class=batch`, `execution_lane=bulk`) so they **cannot** land on the
interactive pool even if a caller forges an interactive hint.

## Legitimate paths

| Path | Why it is not a bypass |
|------|------------------------|
| `tools/platform/documents.py` via ToolGateway | Sole tool entry; capability + tenant checked |
| `DocumentService.ingest` / parsers | Internal; no network; zip_safety bounds OOXML |
| `DocumentIntelligenceService` OCR providers | Fake/Null in tests; production via injected provider |
| Durable SQLite jobs / versions | Tenant-bound; checkpoint stage for crash/resume |
| `classify_workload` + stamped metadata | Trusted keys only; payload cannot downgrade lane |

## Not allowed

- Agent → live OCR / LLM vendor SDK directly (no vendor required for Block 6)
- Parallel document queue outside TaskQueue bulk lane for large OCR
- Separate document store outside `documents/` + Tool Platform
- Payload override of `tenant_id` / trusted job type / execution lane
- Treating prompt-injection text as executable instructions (data only)
- Block 7 SKU/INN fuzzy matching in reconciliation (exact Decimal only)

## Codebase scan notes

- Parsers under `documents/parsers/` are internal and OK for unit tests.
- Production document tools go through ToolGateway descriptors.
- `documents/planner.py` hard-stamps batch admission; interactive hints ignored.
- Observability (`documents/observability.py`) never emits full text, raw OCR,
  table contents, or secrets.
