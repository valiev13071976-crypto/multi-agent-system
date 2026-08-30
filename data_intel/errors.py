"""Excel / Data Intelligence error taxonomy — fail closed."""

from __future__ import annotations


class DataIntelError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        self.error_code = reason
        super().__init__(reason)


UNSUPPORTED_SPREADSHEET = "unsupported_spreadsheet"
STRUCTURE_AMBIGUOUS = "structure_ambiguous"
COLUMN_MAPPING_FAILED = "column_mapping_failed"
INVALID_IDENTIFIER = "invalid_identifier"
DATASET_TOO_LARGE = "dataset_too_large"
DATASET_PARSE_FAILED = "dataset_parse_failed"
MERGE_CONFLICT = "merge_conflict"
RECONCILIATION_CONFLICT = "reconciliation_conflict"
FORMULA_VALIDATION_FAILED = "formula_validation_failed"
EXCEL_GENERATION_FAILED = "excel_generation_failed"
DATASET_ACCESS_DENIED = "dataset_access_denied"
DATASET_NOT_FOUND = "dataset_not_found"
DATASET_STORE_UNAVAILABLE = "dataset_store_unavailable"
LARGE_DATASET_WORKFLOW_UNAVAILABLE = "large_dataset_workflow_unavailable"
DATASET_BATCH_REQUIRED = "dataset_batch_required"
DATASET_TYPE_MISMATCH = "dataset_type_mismatch"
DATASET_CORRUPT = "dataset_corrupt"
DATASET_JOIN_EXPLOSION = "dataset_join_explosion"
DATASET_CURRENCY_MISMATCH = "dataset_currency_mismatch"
DATASET_ENCODING_AMBIGUOUS = "dataset_encoding_ambiguous"
DATASET_RESOURCE_LIMIT = "dataset_resource_limit"
