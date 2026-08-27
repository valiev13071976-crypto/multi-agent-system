"""HITL HTTP approval binding — identity from RequestSecurityContext only."""

from __future__ import annotations

from dataclasses import dataclass

from hitl.authority import InMemoryApprovalAuthority
from hitl.errors import (
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalInvalidStateError,
    ApprovalNotFoundError,
    ApprovalUnauthorizedResolverError,
)
from hitl.models import APPROVAL_CLASS_RANK, APPROVER_ROLE_RANK
from hitl.service import HITLService
from hitl.models import (
    ROLE_HIGH_RISK_APPROVER,
    ROLE_PRIVILEGED_APPROVER,
    ROLE_STANDARD_APPROVER,
)
from security.config import ROLE_ADMIN, ROLE_APPROVER, ROLE_SERVICE
from security.errors import ResourceNotFoundError, UnauthorizedError
from security.identity import RequestSecurityContext
from security.rbac import PERM_HITL_APPROVE, RBACPolicy
from security.resource_auth import ResourceAuthorizer
from security.tenant import workflow_tenant_id


@dataclass(frozen=True)
class HitlActionPayload:
    """Optional backward-compat fields — never trusted over RequestSecurityContext."""

    approver_id: str | None = None
    approver_role: str | None = None
    tenant_id: str | None = None
    expected_version: int | None = None


def _validate_payload_against_context(
    ctx: RequestSecurityContext, payload: HitlActionPayload | None
) -> None:
    if payload is None:
        return
    if payload.approver_id and payload.approver_id.strip() != ctx.user_id:
        raise UnauthorizedError("approver_mismatch")
    if payload.tenant_id and payload.tenant_id.strip() != ctx.tenant_id:
        raise ResourceNotFoundError("approval_not_found")
    if payload.approver_role:
        role = payload.approver_role.strip()
        if role not in ctx.roles:
            raise UnauthorizedError("approver_role_mismatch")


def hitl_role_from_rbac(roles: tuple[str, ...]) -> str | None:
    """Map RBAC roles to HITL approver rank. Admin alone cannot approve."""
    if ROLE_APPROVER in roles:
        if ROLE_ADMIN in roles:
            return ROLE_HIGH_RISK_APPROVER
        return ROLE_STANDARD_APPROVER
    if ROLE_SERVICE in roles:
        return ROLE_STANDARD_APPROVER
    return None


def rbac_satisfies_approval_class(roles: tuple[str, ...], approval_class: str) -> bool:
    hitl_role = hitl_role_from_rbac(roles)
    if hitl_role is None:
        return False
    required = APPROVAL_CLASS_RANK.get(approval_class, 99)
    return APPROVER_ROLE_RANK[hitl_role] >= required


def _sync_authority_grant(authority: InMemoryApprovalAuthority, ctx: RequestSecurityContext) -> str:
    hitl_role = hitl_role_from_rbac(ctx.roles)
    if hitl_role is None:
        raise UnauthorizedError("hitl_authority_denied")
    authority.grant(ctx.user_id, hitl_role)
    return hitl_role


class HitlHttpAuthorizer:
    """Bind external HITL approve/deny to trusted RequestSecurityContext."""

    def __init__(
        self,
        *,
        rbac: RBACPolicy | None = None,
        resource_authorizer: ResourceAuthorizer | None = None,
    ):
        self.rbac = rbac or RBACPolicy()
        self.resources = resource_authorizer or ResourceAuthorizer(self.rbac)

    def authorize_approval_action(
        self,
        ctx: RequestSecurityContext,
        *,
        approval_id: str,
        workflow_id: str,
        hitl: HITLService,
        workflow_state,
        payload: HitlActionPayload | None = None,
    ) -> None:
        _validate_payload_against_context(ctx, payload)
        self.resources.require_permission(ctx, PERM_HITL_APPROVE)
        self.resources.authorize_workflow_access(
            ctx, workflow_state, permission=PERM_HITL_APPROVE
        )
        try:
            record = hitl.get(approval_id)
        except ApprovalNotFoundError as exc:
            raise ResourceNotFoundError("approval_not_found") from exc
        if record.workflow_id != workflow_id:
            raise ResourceNotFoundError("approval_not_found")
        owner = workflow_tenant_id(workflow_state)
        if owner != ctx.tenant_id:
            raise ResourceNotFoundError("approval_not_found")
        if not rbac_satisfies_approval_class(ctx.roles, record.approval_class):
            raise UnauthorizedError("hitl_class_insufficient")
        _sync_authority_grant(hitl.authority, ctx)

    def approve(
        self,
        ctx: RequestSecurityContext,
        *,
        approval_id: str,
        workflow_id: str,
        hitl: HITLService,
        workflow_state,
        payload: HitlActionPayload | None = None,
    ):
        self.authorize_approval_action(
            ctx,
            approval_id=approval_id,
            workflow_id=workflow_id,
            hitl=hitl,
            workflow_state=workflow_state,
            payload=payload,
        )
        expected = payload.expected_version if payload else None
        try:
            return hitl.approve(
                approval_id,
                resolved_by=ctx.user_id,
                expected_version=expected,
            )
        except ApprovalUnauthorizedResolverError as exc:
            raise UnauthorizedError("hitl_authority_denied") from exc
        except ApprovalExpiredError as exc:
            raise UnauthorizedError("approval_expired") from exc
        except ApprovalInvalidStateError as exc:
            raise UnauthorizedError("approval_invalid_state") from exc
        except ApprovalConflictError as exc:
            raise UnauthorizedError("approval_replay") from exc

    def reject(
        self,
        ctx: RequestSecurityContext,
        *,
        approval_id: str,
        workflow_id: str,
        hitl: HITLService,
        workflow_state,
        payload: HitlActionPayload | None = None,
    ):
        self.authorize_approval_action(
            ctx,
            approval_id=approval_id,
            workflow_id=workflow_id,
            hitl=hitl,
            workflow_state=workflow_state,
            payload=payload,
        )
        expected = payload.expected_version if payload else None
        try:
            return hitl.reject(
                approval_id,
                resolved_by=ctx.user_id,
                expected_version=expected,
            )
        except ApprovalUnauthorizedResolverError as exc:
            raise UnauthorizedError("hitl_authority_denied") from exc
        except ApprovalInvalidStateError as exc:
            raise UnauthorizedError("approval_invalid_state") from exc
        except ApprovalConflictError as exc:
            raise UnauthorizedError("approval_replay") from exc
