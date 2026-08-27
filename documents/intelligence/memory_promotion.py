"""Memory promotion interface — controlled writes only, never auto-ingest all docs."""

from __future__ import annotations

from documents.intelligence.contracts import DocumentContent, StructuredDocument
from memory.models import MemoryScope


class DocumentMemoryPromotion:
    """Explicit promotion of document extracts into MemoryService."""

    def __init__(self, memory_service=None):
        self._memory = memory_service

    def available(self) -> bool:
        return self._memory is not None

    def promote_text_chunks(
        self,
        *,
        content: DocumentContent,
        scope: MemoryScope,
        max_chars: int = 8000,
    ) -> dict:
        if self._memory is None:
            return {"promoted": False, "reason": "memory_unavailable"}
        text = (content.text or "")[:max_chars]
        if not text.strip():
            return {"promoted": False, "reason": "empty"}
        # Controlled write — caller must opt in; service methods vary by Memory API
        write = getattr(self._memory, "remember", None) or getattr(self._memory, "store", None)
        if write is None:
            return {"promoted": False, "reason": "memory_api_unavailable"}
        try:
            write(
                scope=scope,
                content=text,
                metadata={
                    "source": "document_intelligence",
                    "document_id": content.document_id,
                    "extraction_method": content.extraction_method,
                },
            )
            return {
                "promoted": True,
                "document_id": content.document_id,
                "chars": len(text),
            }
        except Exception:
            return {"promoted": False, "reason": "promotion_failed"}

    def promote_structured_facts(
        self,
        *,
        structured: StructuredDocument,
        scope: MemoryScope,
    ) -> dict:
        if self._memory is None:
            return {"promoted": False, "reason": "memory_unavailable"}
        facts = {
            "document_type": structured.document_type,
            "identifiers": dict(structured.identifiers),
            "amounts": dict(structured.amounts),
            "dates": dict(structured.dates),
        }
        write = getattr(self._memory, "remember", None) or getattr(self._memory, "store", None)
        if write is None:
            return {"promoted": False, "reason": "memory_api_unavailable"}
        try:
            import json

            write(
                scope=scope,
                content=json.dumps(facts, ensure_ascii=False)[:4000],
                metadata={
                    "source": "document_structured",
                    "document_id": structured.document_id,
                    "schema_version": structured.schema_version,
                },
            )
            return {"promoted": True, "document_id": structured.document_id}
        except Exception:
            return {"promoted": False, "reason": "promotion_failed"}
