"""Knowledge access — same-scope only by default."""

from __future__ import annotations

from memory.models import MemoryScope


OP_READ = "read"
OP_INGEST = "ingest"
OP_REFRESH = "refresh"
OP_REGISTER = "register"
KNOWLEDGE_OPS = (OP_READ, OP_INGEST, OP_REFRESH, OP_REGISTER)


class KnowledgeAccessDenied(PermissionError):
    def __init__(self, reason: str = "knowledge_access_denied"):
        self.reason = reason
        super().__init__(reason)


class KnowledgeAccessPolicy:
    def allow(
        self,
        *,
        requesting: MemoryScope,
        target: MemoryScope,
        operation: str,
    ) -> bool:
        if operation not in KNOWLEDGE_OPS:
            return False
        return requesting.key() == target.key()

    def require(
        self,
        *,
        requesting: MemoryScope,
        target: MemoryScope,
        operation: str,
    ) -> None:
        if not self.allow(requesting=requesting, target=target, operation=operation):
            raise KnowledgeAccessDenied("cross_scope_denied")
