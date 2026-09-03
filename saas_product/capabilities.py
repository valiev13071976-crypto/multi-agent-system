"""Product capability constants — authoritative over browser claims."""

from __future__ import annotations

PERM_PRODUCT_READ = "product:read"
PERM_BILLING_READ = "billing:read"
PERM_BILLING_WRITE = "billing:write"
PERM_MEMBERS_READ = "members:read"
PERM_MEMBERS_WRITE = "members:write"
PERM_PRIVACY_READ = "privacy:read"
PERM_PRIVACY_WRITE = "privacy:write"

ROLE_OWNER = "OWNER"
ROLE_ADMIN = "ADMIN"
ROLE_MEMBER = "MEMBER"
ROLE_USER = "USER"  # canonical human product role; MEMBER retained for compatibility
ROLE_VIEWER = "VIEWER"

VIEWER_PERMS = frozenset({PERM_PRODUCT_READ, PERM_BILLING_READ, PERM_MEMBERS_READ, PERM_PRIVACY_READ})
MEMBER_PERMS = VIEWER_PERMS | frozenset({PERM_PRIVACY_WRITE})
ADMIN_PERMS = MEMBER_PERMS | frozenset({PERM_MEMBERS_WRITE, PERM_BILLING_WRITE})
OWNER_PERMS = ADMIN_PERMS | frozenset({PERM_PRIVACY_WRITE})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_VIEWER: VIEWER_PERMS,
    ROLE_MEMBER: MEMBER_PERMS,
    ROLE_USER: MEMBER_PERMS,
    ROLE_ADMIN: ADMIN_PERMS,
    ROLE_OWNER: OWNER_PERMS,
}
