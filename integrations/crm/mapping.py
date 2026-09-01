"""CRM mapping and duplicate policy."""

from __future__ import annotations

import hashlib

from integrations.crm.errors import CrmAmbiguousTargetError, CrmDuplicateCandidateError


def build_preview(*, operation: str, before: dict | None, after: dict) -> dict:
    return {"operation": operation, "before": before or {}, "after": after, "safe": True}


def check_duplicate_policy(*, matches: list[dict], allow_create: bool = False) -> dict:
    if len(matches) > 1:
        raise CrmAmbiguousTargetError("ambiguous_duplicate_match")
    if len(matches) == 1 and not allow_create:
        return {"status": "DUPLICATE_CANDIDATE", "candidate": matches[0]}
    if len(matches) == 1:
        return {"status": "CREATE_ALLOWED", "warning": "possible_duplicate"}
    return {"status": "CREATE_ALLOWED"}


def apply_patch(existing: dict, patch: dict) -> dict:
    """Omitted keys unchanged; explicit None clears."""
    out = dict(existing)
    for k, v in patch.items():
        out[k] = v
    return out


def fingerprint_crm(*, object_type: str, provider_id: str, patch: dict) -> str:
    payload = f"{object_type}|{provider_id}|" + "|".join(f"{k}={v}" for k, v in sorted(patch.items()))
    return hashlib.sha256(payload.encode()).hexdigest()
