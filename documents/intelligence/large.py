"""Large document processing thresholds — Workflow/TaskQueue path."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LargeDocumentPolicy:
    max_sync_bytes: int = 1_500_000
    max_sync_pages: int = 40
    max_sync_text_chars: int = 400_000

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


def build_large_doc_plan(
    *,
    document_id: str,
    tenant_id: str,
    page_count: int,
    batch_size: int = 10,
) -> dict:
    """Plan page/chunk batches for TaskQueue — does not execute network/LLM."""
    size = max(1, int(batch_size))
    batches = []
    for start in range(1, max(1, page_count) + 1, size):
        end = min(page_count, start + size - 1)
        batches.append(
            {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "page_start": start,
                "page_end": end,
                "execution_key": f"doc-extract:{tenant_id}:{document_id}:{start}-{end}",
            }
        )
    return {
        "document_id": document_id,
        "tenant_id": tenant_id,
        "batch_count": len(batches),
        "batches": batches,
        "workflow_type": "document.large_extract",
    }
