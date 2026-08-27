"""Central RBAC policy — complements AutonomyGate capability tokens."""

from __future__ import annotations

from security.config import (
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_OPERATOR,
    ROLE_SERVICE,
    ROLE_USER,
)

# Permission strings
PERM_ANALYZE_EXECUTE = "analyze:execute"
PERM_WORKFLOW_CREATE = "workflow:create"
PERM_WORKFLOW_READ = "workflow:read"
PERM_WORKFLOW_CANCEL = "workflow:cancel"
PERM_WORKFLOW_RESUME = "workflow:resume"
PERM_HITL_APPROVE = "hitl:approve"
PERM_ADMIN_METADATA = "admin:metadata"
PERM_SERVICE_EXECUTE = "service:execute"

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_USER: frozenset(
        {
            PERM_ANALYZE_EXECUTE,
            PERM_WORKFLOW_CREATE,
            PERM_WORKFLOW_READ,
            PERM_WORKFLOW_CANCEL,
        }
    ),
    ROLE_OPERATOR: frozenset(
        {
            PERM_ANALYZE_EXECUTE,
            PERM_WORKFLOW_CREATE,
            PERM_WORKFLOW_READ,
            PERM_WORKFLOW_CANCEL,
            PERM_WORKFLOW_RESUME,
        }
    ),
    ROLE_APPROVER: frozenset(
        {
            PERM_ANALYZE_EXECUTE,
            PERM_WORKFLOW_CREATE,
            PERM_WORKFLOW_READ,
            PERM_WORKFLOW_CANCEL,
            PERM_WORKFLOW_RESUME,
            PERM_HITL_APPROVE,
        }
    ),
    ROLE_ADMIN: frozenset(
        {
            PERM_ANALYZE_EXECUTE,
            PERM_WORKFLOW_CREATE,
            PERM_WORKFLOW_READ,
            PERM_WORKFLOW_CANCEL,
            PERM_WORKFLOW_RESUME,
            PERM_HITL_APPROVE,
            PERM_ADMIN_METADATA,
        }
    ),
    ROLE_SERVICE: frozenset(
        {
            PERM_SERVICE_EXECUTE,
            PERM_WORKFLOW_CREATE,
            PERM_WORKFLOW_READ,
            PERM_WORKFLOW_CANCEL,
            PERM_WORKFLOW_RESUME,
        }
    ),
}


class RBACDenied(PermissionError):
    def __init__(self, permission: str, reason: str = "rbac_denied"):
        self.permission = permission
        self.reason = reason
        super().__init__(reason)


class RBACPolicy:
    """Single policy owner for HTTP-level RBAC checks."""

    def permissions_for_roles(self, roles: tuple[str, ...]) -> frozenset[str]:
        perms: set[str] = set()
        for role in roles:
            perms.update(ROLE_PERMISSIONS.get(role, frozenset()))
        return frozenset(perms)

    def allow(self, roles: tuple[str, ...], permission: str) -> bool:
        return permission in self.permissions_for_roles(roles)

    def require(self, roles: tuple[str, ...], permission: str) -> None:
        if not self.allow(roles, permission):
            raise RBACDenied(permission)

    def is_admin_metadata_only(self, roles: tuple[str, ...]) -> bool:
        """Admin sees ops metadata, not raw user content by default."""
        return ROLE_ADMIN in roles and ROLE_SERVICE not in roles
