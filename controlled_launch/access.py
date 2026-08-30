"""Controlled launch authorization."""

from __future__ import annotations

from controlled_launch.errors import UNAUTHORIZED, ControlledLaunchError

PERM_LAUNCH_READ = "operations:launch.read"
PERM_LAUNCH_WRITE = "operations:launch.write"


class LaunchAuthorizationPolicy:
    def require(self, ctx, permission: str) -> None:
        perms = set(getattr(ctx, "permissions", ()) or ())
        roles = set(getattr(ctx, "roles", ()) or ())
        if permission in perms:
            return
        if "PLATFORM_ADMIN" in roles and permission.startswith("operations:"):
            return
        if permission == PERM_LAUNCH_READ and ("OPERATOR" in roles or "operations:read" in perms):
            return
        raise ControlledLaunchError(UNAUTHORIZED, details={"permission": permission})
