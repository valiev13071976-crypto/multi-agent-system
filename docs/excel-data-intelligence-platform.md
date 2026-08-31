# Excel / Data Intelligence Platform

## Architecture

One shared tabular intelligence layer for XLS, XLSX, and CSV:

```
Files & Document Intelligence (parse → canonical tables)
            ↓
    DataIntelligenceService.ingest / ingest_from_document
            ↓
Structure detection → Column mapping → Type inference
            ↓
Cleaning / Normalization (raw + normalized preserved)
            ↓
Matching / Dedupe / Compare / Reconcile / Merge
            ↓
Calculations (Decimal) / Aggregation / Validation
            ↓
Report model → XLSX/CSV output (tenant-owned artifact)
```

Heavy workloads:

```
DataJob → DataPlanner (batch/bulk lane) → row batches → checkpoint/resume
```

**Not** separate PriceListSystem, StockSystem, PaymentSystem cores — use cases over shared primitives in `data_intel/`.

## Files & Document handoff

```
DocumentRecord / ParsedDocument
        ↓
DataIntelligenceService.ingest_from_document()
        ↓
data_intel/ingest.py (grid + structure)
```

Do not duplicate XLS/XLSX/CSV parsers. Zip safety via `documents/zip_safety.py`.

## Core modules

| Module | Purpose |
|--------|---------|
| `data_intel/service.py` | Facade orchestration |
| `data_intel/ingest.py` | XLS/XLSX/CSV → grid |
| `data_intel/structure.py` | Header/table detection |
| `data_intel/mapping.py` | Semantic column roles (SKU, INN, price…) |
| `data_intel/types_infer.py` | Type inference (identifier-safe) |
| `data_intel/cleaning.py` | Deterministic normalization |
| `data_intel/identifiers_ru.py` | INN/KPP/OGRN validation |
| `data_intel/counterparty.py` | INN-first counterparty matching |
| `data_intel/product_match.py` | SKU/EAN/MPN matching |
| `data_intel/duplicates.py` | Duplicate groups (non-destructive) |
| `data_intel/compare.py` | Price-list + stock reconciliation |
| `data_intel/reconcile.py` | Payment reconciliation |
| `data_intel/merge.py` | Governed joins with cardinality guard |
| `data_intel/analysis.py` | Margin + anomalies (Decimal) |
| `data_intel/excel_out.py` | XLSX generation |
| `data_intel/formulas.py` | Formula injection safety |
| `data_intel/planner.py` | Batch admission (TaskQueue bulk) |
| `data_intel/large.py` | Large dataset policy, row batches |
| `data_intel/tools.py` | Tool Platform adapter (`data.*`) |

## LLM boundary

LLM may assist **only** with:
- Ambiguous column-name mapping (optional `llm_suggestions` in `mapping.py`)
- Bounded samples

LLM must **never**:
- Process entire large workbooks row-by-row
- Perform joins, totals, reconciliation, or Decimal arithmetic

Enforced by `LargeDatasetPolicy` and deterministic pipelines.

## Money / identifiers

- Financial values use `Decimal`, not float.
- INN/SKU/barcode: leading zeros preserved; never cast to float.
- INN checksum validation in `identifiers_ru.py`.

## Tool Platform

Governed capabilities via `DataIntelToolAdapter`:
`data.ingest`, `data.profile`, `data.normalize`, `data.match`, `data.compare`, `data.reconcile`, `data.merge`, `data.generate_excel`, etc.

Side effects: analysis = read/compute; artifact generation = governed write.

## Security

- Uploaded formulas never executed (`formulas.sanitize_cell_text`)
- Generated formulas only from trusted `validate_formula()` whitelist
- Join many-to-many explosion blocked (`DATASET_JOIN_EXPLOSION`)
- Cross-tenant dataset access fail-closed

## How to add a normalizer

Extend `data_intel/cleaning.py` with explicit, versioned transformation. Preserve raw value alongside normalized output.

## How to add a semantic column role

Add role constant in `data_intel/contracts.py` and alias entries in `data_intel/mapping.py`.

## How to add a matching strategy

Extend shared matching in `counterparty.py` / `product_match.py` / acquisition entity resolution — do not create per-use-case engines.

## How to add a reconciliation type

Implement using `data_intel/reconcile.py` or `compare.py` primitives; return provenance-safe row refs.

## How to add a calculation

Add explicit Decimal formula in `data_intel/analysis.py` with traceable version metadata.

## How to add a report

Build report dict → `excel_out.py` generator; record processing plan version in output provenance.

## Downstream handoff

Marketplace, E-commerce, and 1C modules consume normalized datasets, match results, reconciliation outputs, and exception reports — not raw file parsers.

## Tests

- `tests/test_data_intelligence.py` — core E2E flows
- `tests/test_data_intelligence_block7_platform.py` — platform closure
- `tests/test_data_intel_production_wiring.py` — production compose
- `tests/test_data_intelligence_expansion_closure.py` — applied expansion closure

See `docs/data-intel-bypass-audit.md` for governed path audit.
