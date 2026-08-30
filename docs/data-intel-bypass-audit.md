# Data Intelligence Platform — Bypass Audit (Block 7)

Legitimate low-level paths vs forbidden Agent→spreadsheet shortcuts for
Excel / Data Intelligence (7.1–7.12).

## Governed path (required)

```
Workflow / ToolGateway → DataIntelToolAdapter → DataIntelligenceService
  → ingest → structure → normalize → match/compare/reconcile/merge/analyze
  → excel_out → tenant-scoped dataset store
```

Large datasets stamp **trusted** TaskQueue metadata (`trusted_job_type=data_large|excel_large`,
`workload_class=batch`, `execution_lane=bulk`) so they **cannot** land on the
interactive pool even if a caller forges an interactive hint.

## Legitimate paths

| Path | Why it is not a bypass |
|------|------------------------|
| `data_intel/tools.py` via ToolGateway | Sole data tool entry; capability + tenant checked |
| `data_intel/ingest.py` internal parsers | Internal; zip_safety on XLSX; formula-as-data |
| `data_intel/service.py` | Tenant-bound store; planner batch gates on sync heavy ops |
| Durable SQLite dataset store | Tenant-bound; checkpoint partials for large jobs |
| `data.large_process` workflow | Shared TaskQueue bulk lane |

## Not allowed

- Agent → pandas/openpyxl/xlrd directly (no imports under `agents/`)
- Parallel data queue outside TaskQueue bulk lane
- Separate tenant/artifact store outside governed layers
- Payload override of `tenant_id` / trusted job type / execution lane
- Sync heavy data ops without planner admission (fail closed → `dataset_batch_required`)
- Raw dataset rows in observability logs
- Block 7 SKU fuzzy auto-accept without ambiguity margin (profile-controlled)

## Codebase scan notes

- Internal parser use under `data_intel/` is OK; production tools go through ToolGateway.
- `data_intel/planner.py` hard-stamps batch admission; interactive hints ignored.
- XLSX ingest uses `documents/zip_safety.py` for OOXML package bounds.
- `ExcelContractAdapter` in `tools/platform/contracts.py` is scaffold-only (`enabled=False`).
