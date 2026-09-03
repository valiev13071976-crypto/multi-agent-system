"""Complimentary access grants (ACCESS TYPE — never a role)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from accounts.errors import AccountsError
from accounts.models import AccountsAuditEvent, ComplimentaryAccessRecord, ROLE_OWNER
from accounts.plans import get_plan
from accounts.reasons import OWNER_REQUIRED


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ComplimentaryService:
    def __init__(self, *, store):
        self.store = store

    def grant(
        self,
        *,
        actor_id: str,
        actor_role: str,
        tenant_id: str,
        user_id: str,
        plan_id: str,
        reason: str,
        access_until: str = "",
        unlimited: bool = False,
    ) -> ComplimentaryAccessRecord:
        if actor_role != ROLE_OWNER:
            raise AccountsError(OWNER_REQUIRED)
        if get_plan(plan_id) is None:
            raise AccountsError("INVALID_PLAN")
        if not unlimited and not access_until:
            raise AccountsError("ACCESS_UNTIL_REQUIRED", "Complimentary access requires end date or unlimited flag.")
        grant = ComplimentaryAccessRecord(
            grant_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            plan_id=plan_id,
            access_started_at=_iso(),
            access_until="" if unlimited else access_until,
            unlimited=unlimited,
            reason=reason[:500],
            granted_by=actor_id,
            created_at=_iso(),
        )
        self.store.create_complimentary(grant)
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_iso(),
                actor_id=actor_id,
                target_id=grant.grant_id,
                tenant_id=tenant_id,
                action="complimentary.granted",
                result="ok",
                metadata={"plan_id": plan_id, "user_id": user_id},
            )
        )
        return grant

    def revoke(self, *, actor_id: str, actor_role: str, grant_id: str) -> ComplimentaryAccessRecord:
        if actor_role != ROLE_OWNER:
            raise AccountsError(OWNER_REQUIRED)
        grant = self.store.get_complimentary(grant_id)
        if grant is None:
            raise AccountsError("NOT_FOUND")
        if grant.revoked_at:
            return grant
        updated = ComplimentaryAccessRecord(**{**grant.__dict__, "revoked_at": _iso()})
        self.store.update_complimentary(updated)
        self.store.append_audit(
            AccountsAuditEvent(
                event_id=str(uuid.uuid4()),
                timestamp=_iso(),
                actor_id=actor_id,
                target_id=grant_id,
                tenant_id=grant.tenant_id,
                action="complimentary.revoked",
                result="ok",
            )
        )
        return updated
