"""Canonical document error codes — fail closed, no stack traces to callers."""

from __future__ import annotations


class DocumentError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


UNSUPPORTED_DOCUMENT_TYPE = "unsupported_document_type"
DOCUMENT_TYPE_MISMATCH = "document_type_mismatch"
DOCUMENT_TOO_LARGE = "document_too_large"
DOCUMENT_TOO_MANY_PAGES = "document_too_many_pages"
DOCUMENT_TOO_MANY_SHEETS = "document_too_many_sheets"
DOCUMENT_TOO_MANY_CELLS = "document_too_many_cells"
DOCUMENT_TOO_MANY_CHUNKS = "document_too_many_chunks"
DOCUMENT_PARSE_FAILED = "document_parse_failed"
DOCUMENT_REQUIRES_OCR = "document_requires_ocr"
DOCUMENT_ENCRYPTED = "encrypted_document"
DOCUMENT_MALFORMED = "malformed_document"
OCR_UNAVAILABLE = "ocr_unavailable"
OCR_FAILED = "ocr_failed"
PDF_RASTERIZATION_UNAVAILABLE = "pdf_rasterization_unavailable"
LARGE_DOCUMENT_WORKFLOW_UNAVAILABLE = "large_document_workflow_unavailable"
STRUCTURED_EXTRACTION_FAILED = "structured_extraction_failed"
VALIDATION_FAILED = "validation_failed"
COMPARISON_FAILED = "comparison_failed"
GENERATION_FAILED = "generation_failed"
CONVERSION_UNAVAILABLE = "conversion_unavailable"
CONVERSION_FAILED = "conversion_failed"
DOCUMENT_MACROS_NOT_ALLOWED = "document_macros_not_allowed"
ARCHIVE_EXPANSION_LIMIT_EXCEEDED = "archive_expansion_limit_exceeded"
DOCUMENT_ACCESS_DENIED = "document_access_denied"
DOCUMENT_ENCRYPTION_UNAVAILABLE = "document_encryption_unavailable"
DOCUMENT_STORE_UNAVAILABLE = "document_store_unavailable"
DOCUMENT_RANGE_INVALID = "document_range_invalid"
DOCUMENT_SHEET_NOT_FOUND = "document_sheet_not_found"
DOCUMENT_PATH_DENIED = "document_path_denied"
DOCUMENT_SECRET_DENIED = "document_secret_denied"
DOCUMENT_DISABLED = "document_disabled"
