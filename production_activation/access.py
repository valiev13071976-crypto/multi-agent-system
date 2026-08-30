"""Production activation authorization."""

from __future__ import annotations

PERM_ACTIVATION_READ = "operations:activation.read"
PERM_ACTIVATION_WRITE = "operations:activation.write"
PERM_ACTIVATION_AUTHORIZE = "operations:activation.authorize"


class ActivationAuthorizationPolicy:
    def require(self, ctx, permission: str) -> None:
        from production_activation.errors import UNAUTHORIZED, ProductionActivationError

        perms = set(getattr(ctx, "permissions", ()) or ())
        roles = set(getattr(ctx, "roles", ()) or ())
        if permission in perms:
            return
        if "PLATFORM_ADMIN" in roles and permission.startswith("operations:"):
            return
        if permission == PERM_ACTIVATION_READ and ("OPERATOR" in roles or "operations:read" in perms):
            return
        raise ProductionActivationError(UNAUTHORIZED, details={"permission": permission})
