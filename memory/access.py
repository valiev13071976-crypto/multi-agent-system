"""Memory access policy — default deny cross-scope."""

from __future__ import annotations

from memory.models import MemoryScope


OP_READ = "read"
OP_WRITE = "write"
OP_UPDATE = "update"
OP_DELETE = "delete"
MEMORY_OPS = (OP_READ, OP_WRITE, OP_UPDATE, OP_DELETE)


class MemoryAccessDenied(PermissionError):
    def __init__(self, reason: str = "memory_access_denied"):
        self.reason = reason
        super().__init__(reason)


class MemoryAccessPolicy:
    """Same-scope only by default. No role-as-permission shortcut."""

    def allow(
        self,
        *,
        requesting: MemoryScope,
        target: MemoryScope,
        operation: str,
    ) -> bool:
        if operation not in MEMORY_OPS:
            return False
        # Tenant boundary first
        req_tenant = str(requesting.tenant_ref or "").strip()
        tgt_tenant = str(target.tenant_ref or "").strip()
        if req_tenant and tgt_tenant and req_tenant != tgt_tenant:
            return False
        return requesting.key() == target.key()

    def require(
        self,
        *,
        requesting: MemoryScope,
        target: MemoryScope,
        operation: str,
    ) -> None:
        if not self.allow(
            requesting=requesting, target=target, operation=operation
        ):
            raise MemoryAccessDenied("cross_scope_denied")
