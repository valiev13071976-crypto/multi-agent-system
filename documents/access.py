"""Document access policy — same-scope only by default."""

from __future__ import annotations

from documents.errors import DOCUMENT_ACCESS_DENIED, DocumentError
from memory.models import MemoryScope


OP_INGEST = "ingest"
OP_READ = "read"
OP_EXTRACT = "extract"
OP_DELETE = "delete"
OP_CHUNK = "chunk"
DOCUMENT_OPS = (OP_INGEST, OP_READ, OP_EXTRACT, OP_DELETE, OP_CHUNK)


class DocumentAccessDenied(DocumentError):
    def __init__(self, reason: str = DOCUMENT_ACCESS_DENIED):
        super().__init__(reason)


class DocumentAccessPolicy:
    def allow(
        self,
        *,
        requesting: MemoryScope,
        target: MemoryScope,
        operation: str,
    ) -> bool:
        if operation not in DOCUMENT_OPS:
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
            raise DocumentAccessDenied(DOCUMENT_ACCESS_DENIED)
