"""Data Acquisition & Parsing Platform — error taxonomy (§51)."""


class AcquisitionError(Exception):
    def __init__(self, error_code: str = "acquisition_failed"):
        self.error_code = error_code
        super().__init__(error_code)


# --- Source / policy ---
class SourceUnavailableError(AcquisitionError):
    def __init__(self, error_code: str = "source_unavailable"):
        super().__init__(error_code)


class SourceNotFoundError(AcquisitionError):
    def __init__(self, error_code: str = "source_not_found"):
        super().__init__(error_code)


class SourceRegistryFrozenError(AcquisitionError):
    def __init__(self, error_code: str = "source_registry_frozen"):
        super().__init__(error_code)


class SourceAlreadyRegisteredError(AcquisitionError):
    def __init__(self, error_code: str = "source_already_registered"):
        super().__init__(error_code)


class SourcePolicyDeniedError(AcquisitionError):
    def __init__(self, error_code: str = "source_policy_denied", *, reason: str = ""):
        self.reason = reason or error_code
        super().__init__(error_code)


class SourcePolicyUnknownError(AcquisitionError):
    def __init__(self, error_code: str = "source_policy_unknown", *, reason: str = ""):
        self.reason = reason or error_code
        super().__init__(error_code)


# --- Fetch / network (via ToolGateway) ---
class AcquisitionTimeoutError(AcquisitionError):
    def __init__(self, error_code: str = "acquisition_timeout"):
        super().__init__(error_code)


class AcquisitionDeniedError(AcquisitionError):
    def __init__(self, error_code: str = "acquisition_denied"):
        super().__init__(error_code)


class RateLimitedError(AcquisitionError):
    def __init__(self, error_code: str = "rate_limited", *, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(error_code)


class UnsafeUrlAcquisitionError(AcquisitionError):
    def __init__(self, error_code: str = "unsafe_url"):
        super().__init__(error_code)


class RedirectDeniedError(AcquisitionError):
    def __init__(self, error_code: str = "redirect_denied"):
        super().__init__(error_code)


class CapacityRejectedError(AcquisitionError):
    def __init__(self, error_code: str = "capacity_rejected"):
        super().__init__(error_code)


class JobCancelledError(AcquisitionError):
    def __init__(self, error_code: str = "job_cancelled"):
        super().__init__(error_code)


class JobNotFoundError(AcquisitionError):
    def __init__(self, error_code: str = "job_not_found"):
        super().__init__(error_code)


class CheckpointError(AcquisitionError):
    def __init__(self, error_code: str = "checkpoint_failed"):
        super().__init__(error_code)


# --- Content / parse ---
class UnsupportedContentError(AcquisitionError):
    def __init__(self, error_code: str = "unsupported_content"):
        super().__init__(error_code)


class ContentTooLargeError(AcquisitionError):
    def __init__(self, error_code: str = "content_too_large"):
        super().__init__(error_code)


class ContentNestingTooDeepError(AcquisitionError):
    def __init__(self, error_code: str = "content_nesting_too_deep"):
        super().__init__(error_code)


class EncodingError(AcquisitionError):
    def __init__(self, error_code: str = "encoding_error"):
        super().__init__(error_code)


class ParserNotFoundError(AcquisitionError):
    def __init__(self, error_code: str = "parser_not_found"):
        super().__init__(error_code)


class ParserFailedError(AcquisitionError):
    def __init__(self, error_code: str = "parser_failed"):
        super().__init__(error_code)


class ExtractionStatusError(AcquisitionError):
    """Field extraction reported MISSING/INVALID/EMPTY/UNAVAILABLE."""

    def __init__(self, error_code: str = "extraction_status", *, status: str = ""):
        self.status = status
        super().__init__(error_code)


class InvalidRecordError(AcquisitionError):
    def __init__(self, error_code: str = "invalid_record"):
        super().__init__(error_code)


class IdentifierConflictError(AcquisitionError):
    def __init__(self, error_code: str = "identifier_conflict"):
        super().__init__(error_code)


class DuplicateRecordError(AcquisitionError):
    def __init__(self, error_code: str = "duplicate_record"):
        super().__init__(error_code)


class NormalizationError(AcquisitionError):
    def __init__(self, error_code: str = "normalization_failed"):
        super().__init__(error_code)


class DedupeError(AcquisitionError):
    def __init__(self, error_code: str = "dedupe_failed"):
        super().__init__(error_code)


class IngestionError(AcquisitionError):
    def __init__(self, error_code: str = "ingestion_failed"):
        super().__init__(error_code)


class StaleSourceError(AcquisitionError):
    def __init__(self, error_code: str = "stale_source"):
        super().__init__(error_code)


class TenantIsolationError(AcquisitionError):
    def __init__(self, error_code: str = "tenant_isolation_violation"):
        super().__init__(error_code)


class ArtifactOwnershipError(AcquisitionError):
    def __init__(self, error_code: str = "artifact_ownership_mismatch"):
        super().__init__(error_code)
