"""Files & Document Intelligence package."""

from documents.intelligence.contracts import (
    DocumentComparisonResult,
    DocumentContent,
    DocumentDescriptor,
    StructuredDocument,
)
from documents.intelligence.service import DocumentIntelligenceService, build_document_intelligence
from documents.platform_models import (
    ClassificationResult,
    DocumentProcessingJob,
    DocumentResult,
    OCRPlanDecision,
    ReconciliationResult,
)

__all__ = [
    "ClassificationResult",
    "DocumentComparisonResult",
    "DocumentContent",
    "DocumentDescriptor",
    "DocumentIntelligenceService",
    "DocumentProcessingJob",
    "DocumentResult",
    "OCRPlanDecision",
    "ReconciliationResult",
    "StructuredDocument",
    "build_document_intelligence",
]
