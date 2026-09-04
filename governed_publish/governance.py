"""HITL/capability governance for publication WRITE. Reuses autonomy ApprovalStore."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from autonomy.models import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    ApprovalRecord,
    utc_now,
)
from autonomy.store import InMemoryApprovalStore
from commerce.capabilities import CAP_CATALOG_WRITE
from hitl.models import action_fingerprint
from security.tenant import require_tenant_id

from governed_publish.errors import (
    PUBLISH_ACCESS_DENIED,
    PUBLISH_CAPABILITY_DENIED,
    PUBLISH_STALE,
    GovernedPublishError,
)


class _Fingerprint:
    def __init__(self, key: str):
        self.action_type = "external_publish"
        self.tool_id = "governed_publish.fixture"
        self.operation = "execute_fixture"
        self.resource = key
        self.idempotency_key = key
        self.requested_capabilities = (CAP_CATALOG_WRITE,)
        self.risk_class = "high"
        self.tool_trust_level = "write_external_reversible"
        self.metadata = {"reversible": True}
        self.workflow_id = ""
        self.task_id = ""


def require_write_capability(capabilities: set[str] | tuple[str, ...] | None) -> None:
    caps = set(capabilities or ())
    if CAP_CATALOG_WRITE not in caps:
        raise GovernedPublishError(PUBLISH_CAPABILITY_DENIED)


class PublicationGovernance:
    """Thin HITL adapter — does not execute side effects."""

    def __init__(self, store: InMemoryApprovalStore | None = None) -> None:
        self.store = store or InMemoryApprovalStore()

    def request(
        self,
        *,
        tenant_id: str,
        requested_by: str,
        idempotency_key: str,
        content_version: str,
        snapshot_version: str,
        target: str,
        product_id: str,
        plan_id: str,
    ) -> ApprovalRecord:
        tenant = require_tenant_id(tenant_id)
        fp = action_fingerprint(_Fingerprint(idempotency_key))
        rec = ApprovalRecord(
            approval_id=str(uuid.uuid4()),
            workflow_id=f"govpub:{tenant}:{plan_id}",
            task_id=product_id,
            action_id=idempotency_key,
            decision_id=str(uuid.uuid4()),
            status=APPROVAL_PENDING,
            approved_by="",
            created_at=utc_now(),
            requested_by=requested_by,
            action_fingerprint=fp,
            metadata={
                "tenant_id": tenant,
                "content_version": content_version,
                "snapshot_version": snapshot_version,
                "target": target,
                "product_id": product_id,
                "plan_id": plan_id,
                "mode": "FIXTURE",
            },
        )
        self.store.create(rec)
        return rec

    def get(self, approval_id: str, *, tenant_id: str) -> ApprovalRecord:
        rec = self.store.get(approval_id)
        if rec is None or str(rec.metadata.get("tenant_id")) != require_tenant_id(tenant_id):
            raise GovernedPublishError(PUBLISH_ACCESS_DENIED)
        return rec

    def approve(self, approval_id: str, *, tenant_id: str, actor: str) -> ApprovalRecord:
        rec = self.get(approval_id, tenant_id=tenant_id)
        if rec.requested_by and rec.requested_by == actor:
            raise GovernedPublishError(PUBLISH_ACCESS_DENIED, "self_approval_forbidden")
        if rec.status != APPROVAL_PENDING:
            raise GovernedPublishError(PUBLISH_STALE, rec.status)
        updated = replace(rec, status=APPROVAL_APPROVED, approved_by=actor, resolved_by=actor, resolved_at=utc_now())
        self.store.save(updated)
        return updated

    def reject(self, approval_id: str, *, tenant_id: str, actor: str) -> ApprovalRecord:
        rec = self.get(approval_id, tenant_id=tenant_id)
        updated = replace(rec, status=APPROVAL_REJECTED, resolved_by=actor, resolved_at=utc_now())
        self.store.save(updated)
        return updated

    def assert_valid_for_execute(
        self,
        approval: ApprovalRecord,
        *,
        content_version: str,
        snapshot_version: str,
        tenant_id: str,
    ) -> None:
        if str(approval.metadata.get("tenant_id")) != require_tenant_id(tenant_id):
            raise GovernedPublishError(PUBLISH_ACCESS_DENIED)
        if approval.status != APPROVAL_APPROVED:
            raise GovernedPublishError(PUBLISH_STALE, approval.status)
        if approval.metadata.get("content_version") != content_version:
            raise GovernedPublishError(PUBLISH_STALE, "content_version_mismatch")
        if approval.metadata.get("snapshot_version") != snapshot_version:
            raise GovernedPublishError(PUBLISH_STALE, "target_snapshot_mismatch")
