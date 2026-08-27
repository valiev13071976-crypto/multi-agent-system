"""MIME / extension / magic signature validation."""

from __future__ import annotations

from documents.errors import DOCUMENT_TYPE_MISMATCH, UNSUPPORTED_DOCUMENT_TYPE, DocumentError
from documents.models import (
    DOC_CSV,
    DOC_DOCX,
    DOC_IMAGE,
    DOC_JSON,
    DOC_MD,
    DOC_PDF,
    DOC_TXT,
    DOC_XLS,
    DOC_XLSX,
    DOC_XML,
)


_EXT_MAP = {
    ".txt": DOC_TXT,
    ".md": DOC_MD,
    ".markdown": DOC_MD,
    ".csv": DOC_CSV,
    ".xlsx": DOC_XLSX,
    ".xls": DOC_XLS,
    ".xlsm": "xlsm",
    ".docx": DOC_DOCX,
    ".pdf": DOC_PDF,
    ".json": DOC_JSON,
    ".xml": DOC_XML,
    ".png": DOC_IMAGE,
    ".jpg": DOC_IMAGE,
    ".jpeg": DOC_IMAGE,
    ".tif": DOC_IMAGE,
    ".tiff": DOC_IMAGE,
    ".html": "html",
    ".htm": "html",
}

_MIME_MAP = {
    "text/plain": DOC_TXT,
    "text/markdown": DOC_MD,
    "text/csv": DOC_CSV,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DOC_XLSX,
    "application/vnd.ms-excel": DOC_XLS,
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsm",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOC_DOCX,
    "application/pdf": DOC_PDF,
    "application/json": DOC_JSON,
    "text/json": DOC_JSON,
    "application/xml": DOC_XML,
    "text/xml": DOC_XML,
    "image/png": DOC_IMAGE,
    "image/jpeg": DOC_IMAGE,
    "image/tiff": DOC_IMAGE,
    "text/html": "html",
}

_IMAGE_MAGICS = (
    (b"\x89PNG\r\n\x1a\n", DOC_IMAGE),
    (b"\xff\xd8\xff", DOC_IMAGE),
    (b"II*\x00", DOC_IMAGE),
    (b"MM\x00*", DOC_IMAGE),
)


def _extension(filename: str) -> str:
    name = str(filename or "").lower().rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1]


def detect_magic(data: bytes) -> str | None:
    if data.startswith(b"%PDF"):
        return DOC_PDF
    if data.startswith(b"PK\x03\x04"):
        return "ooxml"
    # Legacy XLS (OLE compound) — D0 CF 11 E0
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return DOC_XLS
    for magic, kind in _IMAGE_MAGICS:
        if data.startswith(magic):
            return kind
    sample = data[:4096]
    if not sample:
        return DOC_TXT
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if b"\x00" in sample:
        return None
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return DOC_JSON
    if stripped.startswith("<?xml") or (stripped.startswith("<") and "xmlns" in stripped[:200].lower()):
        return DOC_XML
    return "text"


def resolve_document_type(
    *,
    filename: str,
    data: bytes,
    declared_media_type: str | None = None,
) -> tuple[str, str]:
    """Return (document_type, media_type). Magic/signature wins over wrong extension."""
    ext = _extension(filename)
    ext_type = _EXT_MAP.get(ext)
    mime = (declared_media_type or "").split(";")[0].strip().lower() or None
    mime_type = _MIME_MAP.get(mime) if mime else None
    magic = detect_magic(data)

    if ext_type == "html" or mime_type == "html":
        raise DocumentError(UNSUPPORTED_DOCUMENT_TYPE)
    if ext_type == "xlsm" or mime_type == "xlsm":
        raise DocumentError("document_macros_not_allowed")

    # Prefer magic when it conflicts with extension (wrong extension)
    if magic == DOC_PDF:
        if ext_type and ext_type != DOC_PDF:
            # Explicit mismatch only when both claim different concrete types
            # and magic is definitive — magic wins for PDF signature
            pass
        resolved = DOC_PDF
        return resolved, "application/pdf"

    if magic == DOC_IMAGE:
        return DOC_IMAGE, mime or "image/png"

    if magic == DOC_XLS:
        if ext_type and ext_type not in {DOC_XLS, None}:
            if ext_type in {DOC_XLSX, DOC_DOCX, DOC_PDF}:
                raise DocumentError(DOCUMENT_TYPE_MISMATCH)
        return DOC_XLS, "application/vnd.ms-excel"

    if magic == "ooxml":
        from documents.zip_safety import inspect_zip_safety

        info = inspect_zip_safety(data)
        names = " ".join(info["names"]).lower()
        if "[content_types].xml" in names and "xl/" in names:
            resolved = DOC_XLSX
        elif "[content_types].xml" in names and "word/" in names:
            resolved = DOC_DOCX
        else:
            raise DocumentError(UNSUPPORTED_DOCUMENT_TYPE)
        if ext_type and ext_type not in {resolved, None}:
            # Wrong extension (e.g. .txt on docx) — magic wins
            if ext_type in {DOC_PDF, DOC_IMAGE, DOC_XLS}:
                raise DocumentError(DOCUMENT_TYPE_MISMATCH)
        media = {
            DOC_XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            DOC_DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }[resolved]
        return resolved, media

    if magic == DOC_JSON:
        return DOC_JSON, "application/json"
    if magic == DOC_XML:
        return DOC_XML, "application/xml"

    resolved = ext_type or mime_type
    if magic == "text" or magic == DOC_TXT:
        if resolved is None:
            if ext in {".csv"} or (data and b"," in data[:200]):
                resolved = DOC_CSV
            elif ext in {".md", ".markdown"}:
                resolved = DOC_MD
            elif ext in {".json"}:
                resolved = DOC_JSON
            elif ext in {".xml"}:
                resolved = DOC_XML
            else:
                resolved = DOC_TXT
        if resolved not in {DOC_TXT, DOC_MD, DOC_CSV, DOC_JSON, DOC_XML}:
            # Extension claims binary but content is text — deny dangerous mismatch
            if resolved in {DOC_PDF, DOC_XLSX, DOC_DOCX, DOC_XLS, DOC_IMAGE}:
                raise DocumentError(DOCUMENT_TYPE_MISMATCH)
            resolved = DOC_TXT
        media = {
            DOC_TXT: "text/plain",
            DOC_MD: "text/markdown",
            DOC_CSV: "text/csv",
            DOC_JSON: "application/json",
            DOC_XML: "application/xml",
        }[resolved]
        return resolved, media

    if resolved is None:
        raise DocumentError(UNSUPPORTED_DOCUMENT_TYPE)
    if mime_type and ext_type and mime_type != ext_type:
        if not {mime_type, ext_type} <= {DOC_TXT, DOC_MD}:
            if not {mime_type, ext_type} <= {DOC_JSON, DOC_TXT}:
                raise DocumentError(DOCUMENT_TYPE_MISMATCH)
    media = mime or {
        DOC_TXT: "text/plain",
        DOC_MD: "text/markdown",
        DOC_CSV: "text/csv",
        DOC_JSON: "application/json",
        DOC_XML: "application/xml",
        DOC_XLS: "application/vnd.ms-excel",
        DOC_IMAGE: "image/png",
        DOC_XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        DOC_DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        DOC_PDF: "application/pdf",
    }.get(resolved, "application/octet-stream")
    if resolved not in {
        DOC_TXT,
        DOC_MD,
        DOC_CSV,
        DOC_XLSX,
        DOC_XLS,
        DOC_DOCX,
        DOC_PDF,
        DOC_JSON,
        DOC_XML,
        DOC_IMAGE,
    }:
        raise DocumentError(UNSUPPORTED_DOCUMENT_TYPE)
    return resolved, media
