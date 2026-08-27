"""Files & Document Intelligence package."""

from documents.intelligence.contracts import (
    DocumentComparisonResult,
    DocumentContent,
    DocumentDescriptor,
    StructuredDocument,
)
from documents.intelligence.service import DocumentIntelligenceService, build_document_intelligence

__all__ = [
    "DocumentComparisonResult",
    "DocumentContent",
    "DocumentDescriptor",
    "DocumentIntelligenceService",
    "StructuredDocument",
    "build_document_intelligence",
]
