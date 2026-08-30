"""Large document processing thresholds — Workflow/TaskQueue path."""

from __future__ import annotations

from dataclasses import dataclass

from documents.errors import DocumentError
from documents.planner import LARGE_OCR_PAGES
from security.tenant import normalize_tenant_id


@dataclass(frozen=True)
class LargeDocumentPolicy:
    max_sync_bytes: int = 1_500_000
    max_sync_pages: int = 40
    max_sync_text_chars: int = 400_000
    pages_per_batch: int = 10
    max_text_chars_per_batch: int = 80_000
    max_rows_per_batch: int = 5_000

    def requires_async(
        self,
        *,
        size_bytes: int = 0,
        page_count: int | None = None,
        text_chars: int = 0,
    ) -> bool:
        if size_bytes > self.max_sync_bytes:
            return True
        if page_count is not None and int(page_count) > self.max_sync_pages:
            return True
        if text_chars > self.max_sync_text_chars:
            return True
        return False


def pdf_inline_ocr_requires_batch(
    data: bytes,
    *,
    limits: dict | None = None,
) -> tuple[bool, int | None]:
    """True when sync PDF ingest must not run inline OCR (pages >= LARGE_OCR_PAGES)."""
    from documents.intelligence.pdf_ocr import extract_pdf_pages_text, peek_pdf_page_count

    try:
        page_count = peek_pdf_page_count(data)
    except DocumentError:
        return True, None

    if page_count < LARGE_OCR_PAGES:
        return False, page_count

    lim = dict(limits or {})
    max_pages = int(lim.get("max_pages", 500))
    max_text = int(lim.get("max_text_bytes", 5_000_000))
    try:
        pages, _ = extract_pdf_pages_text(
            data, max_pages=max_pages, max_text=max_text
        )
    except DocumentError:
        return True, page_count

    ocr_needed = any(not str(text).strip() for text in pages.values())
    if ocr_needed:
        return True, page_count
    return False, page_count


def large_extract_execution_key(tenant_id: str, document_id: str, *, version: str = "1") -> str:
    tid = normalize_tenant_id(tenant_id)
    return f"doc-extract:{tid}:{document_id}:v{version}"


def build_large_doc_plan(
    *,
    document_id: str,
    tenant_id: str,
    page_count: int = 0,
    batch_size: int = 10,
    document_type: str = "pdf",
    text_chars: int = 0,
    max_text_chars_per_batch: int = 80_000,
) -> dict:
    """Plan bounded batches. PDF → page ranges; text → char ranges; sheets → explicit fallback."""
    dtype = (document_type or "pdf").lower()
    if dtype == "pdf" or page_count > 0:
        size = max(1, int(batch_size))
        total = max(1, int(page_count) or 1)
        batches = []
        for idx, start in enumerate(range(1, total + 1, size)):
            end = min(total, start + size - 1)
            batches.append(
                {
                    "batch_index": idx,
                    "document_id": document_id,
                    "tenant_id": tenant_id,
                    "kind": "pdf_pages",
                    "page_start": start,
                    "page_end": end,
                    "bounded": True,
                    "execution_key": f"doc-extract:{tenant_id}:{document_id}:{start}-{end}",
                }
            )
        return {
            "document_id": document_id,
            "tenant_id": tenant_id,
            "page_count": total,
            "batch_count": len(batches),
            "batches": batches,
            "workflow_type": "document.large_extract",
            "execution_key": large_extract_execution_key(tenant_id, document_id),
            "strategy": "pdf_page_batches",
        }

    if dtype in {"txt", "md", "json", "xml", "csv"} and text_chars > 0:
        size = max(1, int(max_text_chars_per_batch))
        batches = []
        idx = 0
        for start in range(0, text_chars, size):
            end = min(text_chars, start + size)
            batches.append(
                {
                    "batch_index": idx,
                    "document_id": document_id,
                    "tenant_id": tenant_id,
                    "kind": "text_range",
                    "char_start": start,
                    "char_end": end,
                    "bounded": True,
                    "execution_key": f"doc-extract:{tenant_id}:{document_id}:c{start}-{end}",
                }
            )
            idx += 1
        return {
            "document_id": document_id,
            "tenant_id": tenant_id,
            "page_count": 0,
            "batch_count": len(batches),
            "batches": batches,
            "workflow_type": "document.large_extract",
            "execution_key": large_extract_execution_key(tenant_id, document_id),
            "strategy": "text_char_batches",
        }

    # Spreadsheet / unknown: explicit single full-parse fallback (not pretend batching)
    return {
        "document_id": document_id,
        "tenant_id": tenant_id,
        "page_count": page_count or 1,
        "batch_count": 1,
        "batches": [
            {
                "batch_index": 0,
                "document_id": document_id,
                "tenant_id": tenant_id,
                "kind": "full_document_fallback",
                "bounded": False,
                "fallback": "parser_lacks_partial_range",
                "execution_key": f"doc-extract:{tenant_id}:{document_id}:full",
            }
        ],
        "workflow_type": "document.large_extract",
        "execution_key": large_extract_execution_key(tenant_id, document_id),
        "strategy": "full_document_fallback",
    }
