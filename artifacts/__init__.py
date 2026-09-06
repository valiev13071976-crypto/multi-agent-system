"""Canonical Files / Artifacts / Attachments contract (Block 3.5).

Single cross-format artifact identity + retrieval/download boundary reused by:
- Business Assistant API uploads and generated-file registration;
- Product Media generated images (thin wrapper, bytes stay in product_media);
- Tool/agent trusted attachment resolution.

Does not duplicate binary storage engines that already exist for images
(product_media) -- it registers a canonical reference over them. For every
other supported kind (pdf/spreadsheet/document/text/generic_file) it is the
one authoritative store.
"""

from artifacts.errors import (
    ARTIFACT_ACCESS_DENIED,
    ARTIFACT_INVALID,
    ARTIFACT_NOT_FOUND,
    ARTIFACT_STORAGE_FAILED,
    ARTIFACT_TOO_LARGE,
    ARTIFACT_TYPE_NOT_ALLOWED,
    ArtifactAccessDeniedError,
    ArtifactError,
    ArtifactInvalidError,
    ArtifactNotFoundError,
    ArtifactStorageFailedError,
    ArtifactTooLargeError,
    ArtifactTypeNotAllowedError,
)
from artifacts.models import (
    ARTIFACT_KINDS,
    KIND_DOCUMENT,
    KIND_GENERIC_FILE,
    KIND_IMAGE,
    KIND_PDF,
    KIND_SPREADSHEET,
    KIND_TEXT,
    SOURCE_EXTERNAL,
    SOURCE_GENERATED,
    SOURCE_UPLOAD,
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_MISSING,
    ArtifactRecord,
    kind_for_mime,
)
from artifacts.service import ArtifactService

__all__ = [
    "ARTIFACT_ACCESS_DENIED",
    "ARTIFACT_INVALID",
    "ARTIFACT_KINDS",
    "ARTIFACT_NOT_FOUND",
    "ARTIFACT_STORAGE_FAILED",
    "ARTIFACT_TOO_LARGE",
    "ARTIFACT_TYPE_NOT_ALLOWED",
    "ArtifactAccessDeniedError",
    "ArtifactError",
    "ArtifactInvalidError",
    "ArtifactNotFoundError",
    "ArtifactRecord",
    "ArtifactService",
    "ArtifactStorageFailedError",
    "ArtifactTooLargeError",
    "ArtifactTypeNotAllowedError",
    "KIND_DOCUMENT",
    "KIND_GENERIC_FILE",
    "KIND_IMAGE",
    "KIND_PDF",
    "KIND_SPREADSHEET",
    "KIND_TEXT",
    "SOURCE_EXTERNAL",
    "SOURCE_GENERATED",
    "SOURCE_UPLOAD",
    "STATUS_ACTIVE",
    "STATUS_DELETED",
    "STATUS_MISSING",
    "kind_for_mime",
]
