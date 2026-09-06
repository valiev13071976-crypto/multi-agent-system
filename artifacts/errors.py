"""Canonical artifact error taxonomy (Block 3.5.1, section 22).

Reused across upload/registration/retrieval/download paths instead of each
call site inventing its own ad hoc error string.
"""

from __future__ import annotations

ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
ARTIFACT_ACCESS_DENIED = "ARTIFACT_ACCESS_DENIED"
ARTIFACT_TOO_LARGE = "ARTIFACT_TOO_LARGE"
ARTIFACT_TYPE_NOT_ALLOWED = "ARTIFACT_TYPE_NOT_ALLOWED"
ARTIFACT_INVALID = "ARTIFACT_INVALID"
ARTIFACT_STORAGE_FAILED = "ARTIFACT_STORAGE_FAILED"


class ArtifactError(Exception):
    """Base class for all canonical artifact-layer failures. Fails closed."""

    code = ARTIFACT_INVALID
    http_status = 422

    def __init__(self, message: str = "", *, code: str | None = None):
        self.code = code or self.code
        self.message = message or self.code
        super().__init__(self.message)


class ArtifactNotFoundError(ArtifactError):
    code = ARTIFACT_NOT_FOUND
    http_status = 404


class ArtifactAccessDeniedError(ArtifactError):
    code = ARTIFACT_ACCESS_DENIED
    http_status = 403


class ArtifactTooLargeError(ArtifactError):
    code = ARTIFACT_TOO_LARGE
    http_status = 413


class ArtifactTypeNotAllowedError(ArtifactError):
    code = ARTIFACT_TYPE_NOT_ALLOWED
    http_status = 422


class ArtifactInvalidError(ArtifactError):
    code = ARTIFACT_INVALID
    http_status = 422


class ArtifactStorageFailedError(ArtifactError):
    code = ARTIFACT_STORAGE_FAILED
    http_status = 500
