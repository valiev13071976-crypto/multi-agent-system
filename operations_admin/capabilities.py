"""Admin capability constants — authoritative over browser claims."""

from __future__ import annotations

PERM_OPS_READ = "operations:read"
PERM_OPS_WRITE = "operations:write"
PERM_OPS_RECOVERY = "operations:recovery"
PERM_OPS_SECURITY_READ = "operations:security.read"
PERM_OPS_COST_READ = "operations:cost.read"
PERM_OPS_COST_WRITE = "operations:cost.write"
PERM_OPS_TENANT_READ = "operations:tenant.read"
PERM_OPS_TENANT_WRITE = "operations:tenant.write"
PERM_OPS_APPROVAL = "operations:approval"
PERM_OPS_ROUTING_WRITE = "operations:routing.write"

PROFILE_VIEWER = "VIEWER"
PROFILE_OPERATOR = "OPERATOR"
PROFILE_SECURITY_AUDITOR = "SECURITY_AUDITOR"
PROFILE_TENANT_ADMIN = "TENANT_ADMIN"
PROFILE_PLATFORM_ADMIN = "PLATFORM_ADMIN"

VIEWER_PERMS = frozenset(
    {
        PERM_OPS_READ,
        PERM_OPS_SECURITY_READ,
        PERM_OPS_COST_READ,
        PERM_OPS_TENANT_READ,
    }
)

OPERATOR_PERMS = VIEWER_PERMS | frozenset(
    {
        PERM_OPS_WRITE,
        PERM_OPS_RECOVERY,
        PERM_OPS_APPROVAL,
    }
)

SECURITY_AUDITOR_PERMS = frozenset({PERM_OPS_SECURITY_READ})

TENANT_ADMIN_PERMS = VIEWER_PERMS | frozenset({PERM_OPS_WRITE, PERM_OPS_RECOVERY, PERM_OPS_APPROVAL})

PLATFORM_ADMIN_PERMS = OPERATOR_PERMS | frozenset(
    {
        PERM_OPS_COST_WRITE,
        PERM_OPS_TENANT_WRITE,
        PERM_OPS_ROUTING_WRITE,
    }
)
