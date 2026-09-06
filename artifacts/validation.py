"""Bounded file security/validation controls (Block 3.5.12, 3.5.15).

Canonical allow-list + magic-byte sniffing + safe filename handling shared by
uploads and generated-artifact registration. Extension is NEVER the sole
security signal -- it is cross-checked against sniffed content where a
signature is known, and downloads always send the server-trusted MIME, not
whatever the client claims.

No malware scanning infrastructure exists in this repository; this module
intentionally does not pretend to provide one (Block 3.5.15).
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from artifacts.errors import (
    ArtifactInvalidError,
    ArtifactTooLargeError,
    ArtifactTypeNotAllowedError,
)
from artifacts.models import KIND_GENERIC_FILE, kind_for_mime

# Same bound as the pre-existing business_assistant_api upload path (kept in
# sync deliberately -- business_assistant_api.uploads imports these
# constants instead of redefining them).
ALLOWED_EXTENSIONS = frozenset(
    {".xlsx", ".xls", ".csv", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".txt"}
)

try:
    MAX_ARTIFACT_BYTES = int(os.environ.get("ARTIFACT_MAX_BYTES") or str(10 * 1024 * 1024))
except ValueError:
    MAX_ARTIFACT_BYTES = 10 * 1024 * 1024

_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}

_UNSAFE_CHARS = re.compile(r"[\\/\x00-\x1f\x7f<>:\"|?*]+")
_WS_COLLAPSE = re.compile(r"\s+")
_MAX_NAME_LEN = 180


def _strip_path(name: str) -> str:
    """Prevent path traversal: keep only the final path component."""

    base = str(name or "").replace("\\", "/")
    base = base.rsplit("/", 1)[-1]
    return base.strip()


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """Unicode-safe (incl. Cyrillic) display/storage filename.

    Preserves letters (any script) and digits; strips control/path/unsafe
    characters. Never trusted as an actual filesystem path component without
    additional per-artifact directory scoping by the caller.
    """

    base = _strip_path(name) or fallback
    base = unicodedata.normalize("NFC", base)
    base = _UNSAFE_CHARS.sub("_", base)
    base = _WS_COLLAPSE.sub(" ", base).strip(" .")
    if not base:
        base = fallback
    if len(base) > _MAX_NAME_LEN:
        stem, ext = os.path.splitext(base)
        keep = _MAX_NAME_LEN - len(ext)
        base = (stem[: max(keep, 1)] + ext) if keep > 0 else base[:_MAX_NAME_LEN]
    return base


def safe_download_filename(name: str, *, fallback: str = "file") -> str:
    """Sanitized filename for Content-Disposition -- ASCII-safe fallback plus
    RFC 5987 UTF-8 form is built by the caller (HTTP layer); this only
    guarantees no header-breaking / control characters remain."""

    cleaned = safe_filename(name, fallback=fallback)
    # Header injection defense-in-depth: strip quotes/semicolons even though
    # safe_filename already removed control chars.
    return cleaned.replace('"', "'").replace(";", "_")


def _sniff_signature(content: bytes) -> str | None:
    """Best-effort magic-byte sniff for the formats this block supports."""

    if not content:
        return None
    head = content[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[0:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        # Generic ZIP container -- xlsx/docx are both zip-based; caller
        # resolves the specific kind from the declared extension since the
        # signature alone cannot distinguish them.
        return "application/zip"
    return None


def _looks_like_text(content: bytes) -> bool:
    if not content:
        return True
    sample = content[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def sniff_mime(content: bytes, *, declared_mime: str, filename: str) -> str:
    """Resolve a server-trusted MIME type. Extension/declared MIME are only
    used when no reliable signature is present (e.g. CSV/TXT/legacy .xls)."""

    ext = Path(str(filename or "")).suffix.lower()
    sig = _sniff_signature(content)
    if sig == "application/zip":
        if ext == ".xlsx":
            return _EXT_MIME[".xlsx"]
        if ext == ".docx":
            return _EXT_MIME[".docx"]
        # Zip signature but unexpected extension -- still safe to store, but
        # do not lie about it being the extension's canonical type.
        return "application/zip"
    if sig:
        return sig
    if ext == ".csv" and _looks_like_text(content):
        return "text/csv"
    if ext == ".txt" and _looks_like_text(content):
        return "text/plain"
    if ext == ".xls":
        return _EXT_MIME[".xls"]
    declared = str(declared_mime or "").strip().lower().split(";", 1)[0]
    if declared:
        return declared
    return _EXT_MIME.get(ext, "application/octet-stream")


def validate_artifact_upload(
    *,
    filename: str,
    content: bytes,
    declared_mime: str = "",
    max_bytes: int | None = None,
) -> tuple[str, str, str]:
    """Validate a candidate upload/generated file.

    Returns ``(safe_name, resolved_mime, kind)``. Raises a canonical
    :class:`~artifacts.errors.ArtifactError` subtype on failure -- never
    silently accepts an unsafe or oversized payload.
    """

    if content is None:
        raise ArtifactInvalidError("empty_content")
    limit = MAX_ARTIFACT_BYTES if max_bytes is None else max_bytes
    if len(content) > limit:
        raise ArtifactTooLargeError("upload_too_large")
    if len(content) == 0:
        raise ArtifactInvalidError("empty_content")
    raw_name = _strip_path(filename) or "upload.bin"
    ext = Path(raw_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ArtifactTypeNotAllowedError("upload_type_not_allowed")
    resolved_mime = sniff_mime(content, declared_mime=declared_mime, filename=raw_name)
    name = safe_filename(raw_name)
    kind = kind_for_mime(resolved_mime, raw_name) or KIND_GENERIC_FILE
    return name, resolved_mime, kind
