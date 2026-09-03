"""HITL WRITE governance facade — WRITE denied by default; binds approval fingerprint."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from operational_activation.status import (
    ACTION_EXTERNAL_WRITE,
    ACTION_HIGH_IMPACT_WRITE,
    ACTION_READ,
    ACTION_SAFE_INTERNAL_WRITE,
    WRITE_APPROVAL_REQUIRED,
    WRITE_APPROVED,
    WRITE_COMPENSATION_REQUIRED,
    WRITE_EXECUTING,
    WRITE_FAILED,
    WRITE_PROPOSED,
    WRITE_SUCCEEDED,
)


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc()).isoformat()


def classify_action(action_type: str) -> str:
    read = {"ANALYTICS_READ", "STOCK_READ", "PRICE_READ", "SEO_ANALYZE", "READ"}
    safe = {"SAFE_INTERNAL_WRITE", "PREPARE_PRICE_UPDATE", "CONTENT_GENERATE", "PREPARE_EMAIL"}
    external = {"MARKETPLACE_PRICE_UPDATE", "EMAIL_SEND", "CRM_UPDATE", "CALENDAR_CREATE", "BITRIX_PRODUCT_UPDATE"}
    if action_type in read:
        return ACTION_READ
    if action_type in safe:
        return ACTION_SAFE_INTERNAL_WRITE
    if action_type in external:
        return ACTION_EXTERNAL_WRITE
    return ACTION_HIGH_IMPACT_WRITE


def fingerprint_params(params: dict[str, Any]) -> str:
    blob = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class WriteProposal:
    proposal_id: str
    tenant_id: str
    actor_id: str
    action: str
    resource: str
    params: dict[str, Any]
    integration: str
    idempotency_key: str
    params_fingerprint: str
    classification: str
    state: str
    created_at: str
    expires_at: str
    approved_at: str = ""
    executed_at: str = ""
    result: str = ""
    approval_id: str = ""


class HitlWriteGovernor:
    """In-memory deterministic WRITE governor for operational activation tests."""

    def __init__(self, *, default_ttl_seconds: int = 3600):
        self.default_ttl_seconds = default_ttl_seconds
        self._proposals: dict[str, WriteProposal] = {}
        self._by_idem: dict[tuple[str, str], str] = {}
        self.real_external_writes = 0  # must remain 0 in this block

    def propose(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        action: str,
        resource: str,
        params: dict[str, Any],
        integration: str,
        idempotency_key: str,
    ) -> WriteProposal:
        key = (tenant_id, idempotency_key)
        if key in self._by_idem:
            return self._proposals[self._by_idem[key]]
        classification = classify_action(action)
        now = _utc()
        state = WRITE_PROPOSED if classification == ACTION_READ else WRITE_APPROVAL_REQUIRED
        if classification in {ACTION_EXTERNAL_WRITE, ACTION_HIGH_IMPACT_WRITE}:
            state = WRITE_APPROVAL_REQUIRED
        prop = WriteProposal(
            proposal_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            resource=resource,
            params=dict(params),
            integration=integration,
            idempotency_key=idempotency_key,
            params_fingerprint=fingerprint_params(params),
            classification=classification,
            state=state,
            created_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=self.default_ttl_seconds)),
        )
        self._proposals[prop.proposal_id] = prop
        self._by_idem[key] = prop.proposal_id
        return prop

    def approve(
        self,
        *,
        proposal_id: str,
        approver_id: str,
        tenant_id: str,
        expected_fingerprint: str | None = None,
    ) -> WriteProposal:
        prop = self._require(proposal_id)
        if prop.tenant_id != tenant_id:
            raise PermissionError("TENANT_SCOPE_DENIED")
        if prop.state not in {WRITE_APPROVAL_REQUIRED, WRITE_PROPOSED}:
            raise ValueError("invalid_state")
        if _utc() > datetime.fromisoformat(prop.expires_at):
            prop.state = WRITE_FAILED
            prop.result = "expired"
            return prop
        if expected_fingerprint and expected_fingerprint != prop.params_fingerprint:
            raise ValueError("fingerprint_mismatch")
        prop.state = WRITE_APPROVED
        prop.approved_at = _iso()
        prop.approval_id = f"apr-{approver_id}-{prop.proposal_id[:8]}"
        return prop

    def execute(self, *, proposal_id: str, tenant_id: str, params_now: dict[str, Any] | None = None) -> WriteProposal:
        prop = self._require(proposal_id)
        if prop.tenant_id != tenant_id:
            raise PermissionError("TENANT_SCOPE_DENIED")
        if prop.state != WRITE_APPROVED:
            raise ValueError("not_approved")
        if _utc() > datetime.fromisoformat(prop.expires_at):
            prop.state = WRITE_FAILED
            prop.result = "expired"
            return prop
        if params_now is not None and fingerprint_params(params_now) != prop.params_fingerprint:
            prop.state = WRITE_FAILED
            prop.result = "material_parameter_mutation"
            return prop
        # External WRITE remains denied in this activation block
        if prop.classification in {ACTION_EXTERNAL_WRITE, ACTION_HIGH_IMPACT_WRITE}:
            prop.state = WRITE_FAILED
            prop.result = "REAL_EXTERNAL_WRITE_DISABLED"
            return prop
        prop.state = WRITE_EXECUTING
        prop.executed_at = _iso()
        prop.state = WRITE_SUCCEEDED
        prop.result = "fixture_ok"
        return prop

    def _require(self, proposal_id: str) -> WriteProposal:
        prop = self._proposals.get(proposal_id)
        if prop is None:
            raise KeyError("not_found")
        return prop
