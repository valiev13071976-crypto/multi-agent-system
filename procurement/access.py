"""Same-scope access for procurement entities."""

from __future__ import annotations

from procurement.errors import PROCUREMENT_SCOPE_DENIED, ProcurementError


OP_READ = "read"
OP_WRITE = "write"
OP_APPROVE = "approve"


class ProcurementAccessDenied(ProcurementError):
    def __init__(self, reason: str = PROCUREMENT_SCOPE_DENIED):
        super().__init__(reason)


class ProcurementAccessPolicy:
    def require(self, *, requesting, target, operation: str) -> None:
        _ = operation
        if requesting is None or target is None:
            raise ProcurementAccessDenied(PROCUREMENT_SCOPE_DENIED)
        if requesting.key() != target.key():
            raise ProcurementAccessDenied(PROCUREMENT_SCOPE_DENIED)
