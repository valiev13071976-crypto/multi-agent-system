"""KnowledgeValidator — provenance/trust/freshness/bounds."""

from __future__ import annotations

from knowledge.models import KnowledgeItem, KnowledgeIngestRequest, KnowledgeResult


class KnowledgeValidationError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class KnowledgeValidator:
    def validate_ingest(self, request: KnowledgeIngestRequest, *, max_bytes: int) -> None:
        content = str(request.content or "")
        if not content.strip():
            raise KnowledgeValidationError("knowledge_content_empty")
        if len(content.encode("utf-8")) > int(max_bytes):
            raise KnowledgeValidationError("knowledge_item_too_large")
        if not request.provenance_source_ref.strip():
            raise KnowledgeValidationError("provenance_required")
        if not request.source_id.strip():
            raise KnowledgeValidationError("source_id_required")

    def validate_item(self, item: KnowledgeItem) -> None:
        if not item.provenance.source_id or not item.provenance.source_ref:
            raise KnowledgeValidationError("provenance_required")
        if item.citation_ref.startswith("knowledge:unknown"):
            raise KnowledgeValidationError("citation_unavailable")

    def validate_result(self, result: KnowledgeResult) -> None:
        if not result.citation_ref:
            raise KnowledgeValidationError("citation_unavailable")
        if not result.provenance.source_ref:
            raise KnowledgeValidationError("provenance_required")
