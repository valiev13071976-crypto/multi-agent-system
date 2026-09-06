"""Canonical artifact/file identity (Block 3.5.1).

One record shape for user uploads, tool/agent-generated files, and thin
wrappers over externally-stored artifacts (e.g. product_media images).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

# --- Artifact kind taxonomy (UI/tool routing only -- not a storage format) ---
KIND_IMAGE = "image"
KIND_PDF = "pdf"
KIND_SPREADSHEET = "spreadsheet"
KIND_DOCUMENT = "document"
KIND_TEXT = "text"
KIND_GENERIC_FILE = "generic_file"

ARTIFACT_KINDS = frozenset(
    {KIND_IMAGE, KIND_PDF, KIND_SPREADSHEET, KIND_DOCUMENT, KIND_TEXT, KIND_GENERIC_FILE}
)

# --- Lifecycle status (3.5.13) ---
STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"
STATUS_MISSING = "missing"

ARTIFACT_STATUSES = frozenset({STATUS_ACTIVE, STATUS_DELETED, STATUS_MISSING})

# --- Provenance / source (3.5.1, 3.5.11) ---
SOURCE_UPLOAD = "upload"
SOURCE_GENERATED = "generated"
SOURCE_EXTERNAL = "external"  # bytes owned by another canonical store (e.g. product_media)

ARTIFACT_SOURCES = frozenset({SOURCE_UPLOAD, SOURCE_GENERATED, SOURCE_EXTERNAL})

_EXT_KIND = {
    ".png": KIND_IMAGE,
    ".jpg": KIND_IMAGE,
    ".jpeg": KIND_IMAGE,
    ".webp": KIND_IMAGE,
    ".gif": KIND_IMAGE,
    ".pdf": KIND_PDF,
    ".xlsx": KIND_SPREADSHEET,
    ".xls": KIND_SPREADSHEET,
    ".csv": KIND_SPREADSHEET,
    ".docx": KIND_DOCUMENT,
    ".doc": KIND_DOCUMENT,
    ".txt": KIND_TEXT,
}

_MIME_KIND = {
    "image/png": KIND_IMAGE,
    "image/jpeg": KIND_IMAGE,
    "image/jpg": KIND_IMAGE,
    "image/webp": KIND_IMAGE,
    "image/gif": KIND_IMAGE,
    "application/pdf": KIND_PDF,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": KIND_SPREADSHEET,
    "application/vnd.ms-excel": KIND_SPREADSHEET,
    "text/csv": KIND_SPREADSHEET,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": KIND_DOCUMENT,
    "application/msword": KIND_DOCUMENT,
    "text/plain": KIND_TEXT,
}


def kind_for_mime(mime_type: str, filename: str = "") -> str:
    """Classify an artifact kind for UI/tool routing (never trust extension alone
    for security -- this is a display/routing hint, not an access-control gate)."""

    mime = str(mime_type or "").strip().lower().split(";", 1)[0]
    if mime in _MIME_KIND:
        return _MIME_KIND[mime]
    if mime.startswith("image/"):
        return KIND_IMAGE
    if mime.startswith("text/"):
        return KIND_TEXT
    from pathlib import Path

    ext = Path(str(filename or "")).suffix.lower()
    if ext in _EXT_KIND:
        return _EXT_KIND[ext]
    return KIND_GENERIC_FILE


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ArtifactRecord:
    """Canonical artifact/file identity (3.5.1).

    ``storage_ref`` is an internal locator (e.g. ``sqlite:blob`` or
    ``product_media:{version_id}``) -- never exposed to clients directly.
    Public responses use :meth:`as_public_dict`.
    """

    artifact_id: str
    tenant_id: str
    owner_id: str
    filename: str
    safe_filename: str
    mime_type: str
    size_bytes: int
    kind: str
    created_at: str
    storage_ref: str
    source: str
    status: str = STATUS_ACTIVE
    conversation_id: str = ""
    message_id: str = ""
    request_id: str = ""
    tool_id: str = ""
    derived_from_artifact_id: str = ""
    content_hash: str = ""
    legacy_ref: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def as_public_dict(self) -> dict[str, Any]:
        """Safe, client-facing metadata -- no storage path/internal locator leakage."""

        return {
            "artifact_id": self.artifact_id,
            "filename": self.safe_filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "created_at": self.created_at,
            "source": self.source,
            "status": self.status,
            "conversation_id": self.conversation_id,
            "derived_from_artifact_id": self.derived_from_artifact_id,
            "view_url": f"/api/v1/business-assistant/artifacts/{self.artifact_id}/view",
            "download_url": f"/api/v1/business-assistant/artifacts/{self.artifact_id}/download",
        }


def new_created_at() -> str:
    return _utc_iso()
