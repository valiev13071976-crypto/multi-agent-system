"""Secure upload handling for Business Assistant API inputs.

Allow-list/size-limit/safe-filename logic now lives in the canonical
``artifacts.validation`` module (Block 3.5) -- re-exported here so existing
imports of these names keep working unchanged.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from artifacts.validation import ALLOWED_EXTENSIONS, MAX_ARTIFACT_BYTES as MAX_UPLOAD_BYTES
from artifacts.validation import safe_filename

__all__ = ["ALLOWED_EXTENSIONS", "MAX_UPLOAD_BYTES", "safe_filename", "save_upload"]


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
