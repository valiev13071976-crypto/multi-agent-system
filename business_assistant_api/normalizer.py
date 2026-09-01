"""Request normalization — validation only, no domain logic."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from business_assistant_api.errors import BAA_INVALID_REQUEST, BAA_UNSUPPORTED_CAPABILITY, BusinessAssistantApiError

_ALLOWED_CAPABILITIES = frozenset(
    {
        "data.ingest",
        "data.compare",
        "marketplace.product",
        "cms.bitrix",
        "email",
        "erp.1c",
        "commerce.product",
        "seo",
        "document.compare",
    }
)

_ARTIFACT_REF_RE = re.compile(r"^[a-zA-Z0-9._:/-]{1,256}$")


@dataclass(frozen=True)
class NormalizedSubmission:
    message: str
    artifact_refs: tuple[str, ...]
    requested_capability: str
    conversation_id: str
    idempotency_key: str
    read_only: bool
    priority: str
    metadata: dict
    payload_hash: str


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize_submission(
    *,
    message: str,
    artifact_refs: list[str] | None = None,
    requested_capability: str | None = None,
    conversation_id: str | None = None,
    idempotency_key: str | None = None,
    read_only: bool = False,
    priority: str | None = None,
    metadata: dict | None = None,
    tenant_id: str | None = None,
    owner_id: str | None = None,
) -> NormalizedSubmission:
    # Reject trusted identity overrides from body
    if tenant_id or owner_id:
        raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "identity_fields_not_allowed_in_body", http_status=400)

    text = (message or "").strip()
    if not text or len(text) > 30000:
        raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "message_invalid", http_status=400)

    refs = tuple(r.strip() for r in (artifact_refs or []) if r and r.strip())
    for ref in refs:
        if not _ARTIFACT_REF_RE.match(ref):
            raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "artifact_ref_invalid", http_status=400)

    cap = (requested_capability or "").strip()
    if cap and cap not in _ALLOWED_CAPABILITIES:
        raise BusinessAssistantApiError(BAA_UNSUPPORTED_CAPABILITY, cap, http_status=422)

    conv = (conversation_id or "").strip()
    idem = (idempotency_key or "").strip()
    if idem and (len(idem) < 8 or len(idem) > 128):
        raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "idempotency_key_invalid", http_status=400)

    meta = dict(metadata or {})
    if len(json.dumps(meta)) > 4096:
        raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "metadata_too_large", http_status=400)

    prio = (priority or "normal").strip().lower()
    if prio not in {"low", "normal", "high"}:
        raise BusinessAssistantApiError(BAA_INVALID_REQUEST, "priority_invalid", http_status=400)

    payload = {
        "message": text,
        "artifact_refs": list(refs),
        "requested_capability": cap,
        "read_only": read_only,
        "priority": prio,
    }
    return NormalizedSubmission(
        message=text,
        artifact_refs=refs,
        requested_capability=cap,
        conversation_id=conv,
        idempotency_key=idem,
        read_only=read_only,
        priority=prio,
        metadata=meta,
        payload_hash=_payload_hash(payload),
    )
