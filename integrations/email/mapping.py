"""Email mapping and validation."""

from __future__ import annotations

import re

from integrations.email.errors import EmailAmbiguousRecipientError, EmailAttachmentError, EmailInvalidRecipientError

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def build_preview(*, operation: str, before: dict | None, after: dict) -> dict:
    return {"operation": operation, "before": before or {}, "after": after, "safe": True}


def validate_recipients(*, to: list[str], cc: list | None = None, bcc: list | None = None, ambiguous: bool = False) -> dict:
    if ambiguous:
        raise EmailAmbiguousRecipientError("ambiguous_recipient")
    all_rcpt = list(to or []) + list(cc or []) + list(bcc or [])
    if not all_rcpt:
        raise EmailInvalidRecipientError("empty_recipient")
    for addr in all_rcpt:
        if not _EMAIL_RE.match(str(addr).strip()):
            raise EmailInvalidRecipientError(f"invalid_recipient:{addr}")
    return {"valid": True, "count": len(all_rcpt)}


def validate_attachment_ref(*, attachment_ref: str, tenant_id: str) -> dict:
    ref = str(attachment_ref or "").strip()
    if not ref:
        raise EmailAttachmentError("attachment_required")
    if ".." in ref or ref.startswith("/") or ref.startswith("\\"):
        raise EmailAttachmentError("path_traversal_forbidden")
    if not ref.startswith(f"file:{tenant_id}:"):
        raise EmailAttachmentError("cross_tenant_attachment_forbidden")
    return {"valid": True, "ref": ref}


def fingerprint_email(*, to: list, subject: str, body: str, attachments: list | None = None) -> str:
    import hashlib

    payload = "|".join([",".join(to or []), subject or "", body or "", ",".join(attachments or [])])
    return hashlib.sha256(payload.encode()).hexdigest()
