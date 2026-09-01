"""Secure upload handling for Business Assistant API inputs."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

ALLOWED_EXTENSIONS = frozenset({".xlsx", ".xls", ".csv", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".txt"})
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def safe_filename(name: str) -> str:
    base = Path(name or "upload.bin").name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "upload.bin"
    return cleaned[:200]


def save_upload(
    *,
    base_dir: str,
    tenant_id: str,
    owner_id: str,
    filename: str,
    content: bytes,
    mime_type: str,
) -> dict:
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("upload_too_large")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("upload_type_not_allowed")
    upload_id = str(uuid.uuid4())
    safe = safe_filename(filename)
    tenant_dir = Path(base_dir) / tenant_id / upload_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    target = tenant_dir / safe
    target.write_bytes(content)
    ref = f"artifact://upload/{upload_id}/{safe}"
    return {
        "artifact_ref": ref,
        "upload_id": upload_id,
        "filename": safe,
        "size_bytes": len(content),
        "mime_type": mime_type or "application/octet-stream",
        "tenant_id": tenant_id,
        "owner_id": owner_id,
    }
