"""Data Acquisition & Parsing Platform — error taxonomy."""


class AcquisitionError(Exception):
    def __init__(self, error_code: str = "acquisition_failed"):
        self.error_code = error_code
        super().__init__(error_code)


class SourceUnavailableError(AcquisitionError):
    def __init__(self, error_code: str = "source_unavailable"):
        super().__init__(error_code)


class AcquisitionTimeoutError(AcquisitionError):
    def __init__(self, error_code: str = "acquisition_timeout"):
        super().__init__(error_code)


class AcquisitionDeniedError(AcquisitionError):
    def __init__(self, error_code: str = "acquisition_denied"):
        super().__init__(error_code)


class UnsupportedContentError(AcquisitionError):
    def __init__(self, error_code: str = "unsupported_content"):
        super().__init__(error_code)


class ParserNotFoundError(AcquisitionError):
    def __init__(self, error_code: str = "parser_not_found"):
        super().__init__(error_code)


class ParserFailedError(AcquisitionError):
    def __init__(self, error_code: str = "parser_failed"):
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


class StaleSourceError(AcquisitionError):
    def __init__(self, error_code: str = "stale_source"):
        super().__init__(error_code)


class RateLimitedError(AcquisitionError):
    def __init__(self, error_code: str = "rate_limited"):
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
