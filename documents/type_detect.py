"""MIME / extension / magic signature validation."""

from __future__ import annotations

from documents.errors import DOCUMENT_TYPE_MISMATCH, UNSUPPORTED_DOCUMENT_TYPE, DocumentError
from documents.models import DOC_CSV, DOC_DOCX, DOC_MD, DOC_PDF, DOC_TXT, DOC_XLSX


_EXT_MAP = {
    ".txt": DOC_TXT,
    ".md": DOC_MD,
    ".markdown": DOC_MD,
    ".csv": DOC_CSV,
    ".xlsx": DOC_XLSX,
    ".xlsm": "xlsm",
    ".docx": DOC_DOCX,
    ".pdf": DOC_PDF,
    ".html": "html",
    ".htm": "html",
}

_MIME_MAP = {
    "text/plain": DOC_TXT,
    "text/markdown": DOC_MD,
    "text/csv": DOC_CSV,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DOC_XLSX,
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsm",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOC_DOCX,
    "application/pdf": DOC_PDF,
    "text/html": "html",
}


def _extension(filename: str) -> str:
    name = str(filename or "").lower().rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1]


def detect_magic(data: bytes) -> str | None:
    if data.startswith(b"%PDF"):
        return DOC_PDF
    if data.startswith(b"PK\x03\x04"):
        # OOXML zip — distinguish xlsx vs docx via entries later
        return "ooxml"
    # UTF-8 / ASCII text heuristic
    sample = data[:4096]
    if not sample:
        return DOC_TXT
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if b"\x00" in sample:
        return None
    return "text"


def resolve_document_type(
    *,
    filename: str,
    data: bytes,
    declared_media_type: str | None = None,
) -> tuple[str, str]:
    """Return (document_type, media_type). Raises DocumentError on mismatch/unsupported."""
    ext = _extension(filename)
    ext_type = _EXT_MAP.get(ext)
    mime = (declared_media_type or "").split(";")[0].strip().lower() or None
    mime_type = _MIME_MAP.get(mime) if mime else None
    magic = detect_magic(data)

    if ext_type == "html" or mime_type == "html":
        raise DocumentError(UNSUPPORTED_DOCUMENT_TYPE)
    if ext_type == "xlsm" or mime_type == "xlsm":
        raise DocumentError("document_macros_not_allowed")

    resolved = ext_type
    if mime_type and ext_type and mime_type != ext_type:
        # allow md/txt overlap
        if not {mime_type, ext_type} <= {DOC_TXT, DOC_MD}:
            raise DocumentError(DOCUMENT_TYPE_MISMATCH)

    if magic == DOC_PDF:
        if resolved and resolved != DOC_PDF:
            raise DocumentError(DOCUMENT_TYPE_MISMATCH)
        resolved = DOC_PDF
        media = "application/pdf"
    elif magic == "ooxml":
        if resolved not in {DOC_XLSX, DOC_DOCX, None}:
            raise DocumentError(DOCUMENT_TYPE_MISMATCH)
        if resolved is None:
            # Inspect zip names
            from documents.zip_safety import inspect_zip_safety

            info = inspect_zip_safety(data)
            names = " ".join(info["names"]).lower()
            if "[content_types].xml" in names and "xl/" in names:
                resolved = DOC_XLSX
            elif "[content_types].xml" in names and "word/" in names:
                resolved = DOC_DOCX
            else:
                raise DocumentError(UNSUPPORTED_DOCUMENT_TYPE)
        media = {
            DOC_XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            DOC_DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }[resolved]
    elif magic == "text" or magic == DOC_TXT:
        if resolved is None:
            if ext in {".csv"} or (data and b"," in data[:200]):
                resolved = DOC_CSV
            elif ext in {".md", ".markdown"}:
                resolved = DOC_MD
            else:
                resolved = DOC_TXT
        if resolved not in {DOC_TXT, DOC_MD, DOC_CSV}:
            raise DocumentError(DOCUMENT_TYPE_MISMATCH)
        media = {
            DOC_TXT: "text/plain",
            DOC_MD: "text/markdown",
            DOC_CSV: "text/csv",
        }[resolved]
    else:
        if resolved is None:
            raise DocumentError(UNSUPPORTED_DOCUMENT_TYPE)
        media = mime or "application/octet-stream"

    if resolved not in {DOC_TXT, DOC_MD, DOC_CSV, DOC_XLSX, DOC_DOCX, DOC_PDF}:
        raise DocumentError(UNSUPPORTED_DOCUMENT_TYPE)
    return resolved, media
