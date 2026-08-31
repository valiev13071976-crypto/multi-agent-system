# Files & Document Intelligence

## Architecture

One shared platform for uploads, acquired documents, workflow artifacts, parsing, OCR, indexing, search, controlled RAG, comparison, and generation:

```
FILE SOURCE (upload / workflow / acquisition)
        ↓
Governed File Intake (DocumentIngestRequest)
        ↓
Validation / Security / Tenant ownership
        ↓
Source Artifact (DocumentRecord, content_hash)
        ↓
Format Detection (type_detect)
        ↓
DocumentParserRegistry → ParsedDocument
        ↓
DocumentIntelligenceService (OCR / classify / extract / compare / generate)
        ↓
Chunker → Chunks (provenance-safe)
        ↓
KnowledgeIndex (optional, policy-controlled)
        ↓
Authorized Retrieval / Controlled RAG
        ↓
Answer + citations / governed output artifact
```

**Not** separate PDF/DOCX/OCR/RAG cores — capabilities of one layer.

## Tool Platform integration

```
Agent / Workflow
        ↓
Tool Registry → Tool Router → Tool Gateway
        ↓
DocumentToolAdapter (document.*)
        ↓
DocumentService / DocumentIntelligenceService
```

Uses closed platform: capabilities, data scope, tenant isolation, side effects, audit, artifacts.

## Data Acquisition handoff

```
Data Acquisition → RawArtifact / artifact ref
        ↓
acquisition/parsers/document_bridge.py
        ↓
Document ingest (SOURCE_ACQUIRED) — no independent re-download
```

## Core contracts

| Concept | Module |
|---------|--------|
| File intake | `documents/models.py` — `DocumentIngestRequest` |
| Source categories | `documents/intake_sources.py` |
| Canonical document | `ParsedDocument`, `DocumentRecord` |
| Intelligence contracts | `documents/intelligence/contracts.py` |
| Parser registry | `documents/parsers/__init__.py` |
| OCR | `documents/intelligence/ocr.py` |
| Chunking | `documents/chunker.py` |
| Knowledge / RAG | `knowledge/` — index, retrieval, `rag_context.py` |
| Comparison | `documents/intelligence/compare.py` |
| Generation | `documents/intelligence/generate.py` |
| Errors | `documents/errors.py`, `knowledge/errors.py` |

## Supported formats

| Format | Parser | Notes |
|--------|--------|-------|
| PDF | `pdf.py` | Text + scan detection; OCR via `pdf_ocr.py` |
| DOCX | `docx_parser.py` | Structure, tables; macros rejected |
| TXT / MD | `txt.py`, `md.py` | Line-range provenance |
| CSV | `csv_parser.py` | Delimiter, headers, row provenance |
| XLSX / XLS | `xlsx.py`, `xls.py` | Sheets/cells; formulas as data, never executed |
| PNG/JPEG/TIFF | `image_parser.py` | OCR handoff |
| JSON / XML | `json_parser.py`, `xml_parser.py` | Bounded; XXE denied |

HTML and macro-enabled XLSM are **rejected** at type detection (security).

## Security boundaries

- All files are **untrusted data** — never executed (macros, scripts, formulas, OLE).
- ZIP container safety: `documents/zip_safety.py`
- Size/page/sheet/chunk limits in models and parsers
- Cross-tenant access fail-closed via `documents/access.py` and SQLite `tenant_ref`
- RAG content marked `untrusted_data` in `knowledge/rag_context.py`

## Controlled RAG

```
Query → authorization + tenant/data scope
      → KnowledgeService.retrieve()
      → bounded evidence set
      → RAG context (citations, untrusted flags)
      → LLM answer
```

Insufficient evidence returns explicit flag — no fabrication from unrelated index material.

## Batch routing

Large OCR, bulk ingest, mass compare/generate stamped batch/bulk via `documents/planner.py` and TaskQueue.

## How to add a new document parser

1. Implement `DocumentParser` protocol (`parser_id`, `version`, `supported_types`, `parse()`).
2. Register in `build_default_registry()`.
3. Add type detection in `type_detect.py` if new format.
4. Add contract tests in `tests/test_document_intelligence.py`.

## How to add a new OCR provider

1. Implement `OCRProvider` in `documents/intelligence/ocr.py`.
2. Wire via `build_ocr_provider(env)` in `documents/runtime.py`.
3. Test with fake/deterministic provider.

## Excel / Data Intelligence handoff

XLSX/CSV parse into canonical tables with sheet/cell provenance and formula metadata (expression + cached value). Business reconciliation remains the Excel/Data Intelligence module.

## Tests

- `tests/test_document_*.py` — parsing, security, block6 closure
- `tests/test_knowledge_*.py` — indexing, RAG, block8 closure
- `tests/test_document_expansion_closure.py` — applied expansion closure

See `docs/document-bypass-audit.md` and `docs/knowledge-bypass-audit.md`.
