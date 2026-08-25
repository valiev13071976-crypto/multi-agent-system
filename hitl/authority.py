from hitl.models import (
    APPROVAL_CLASS_RANK,
    APPROVER_ROLE_RANK,
    ROLE_CRITICAL_APPROVER,
    ROLE_HIGH_RISK_APPROVER,
    ROLE_PRIVILEGED_APPROVER,
    ROLE_STANDARD_APPROVER,
)


class ApprovalAuthority:
    def can_resolve(self, subject_id: str, approval_class: str) -> bool:
        raise NotImplementedError


class InMemoryApprovalAuthority(ApprovalAuthority):
    """Default deny unless subject is explicitly granted a role."""

    def __init__(self, grants: dict[str, str] | None = None):
        self._grants = dict(grants or {})

    def grant(self, subject_id: str, role: str) -> None:
        if role not in APPROVER_ROLE_RANK:
            raise ValueError(role)
        self._grants[subject_id] = role

    def can_resolve(self, subject_id: str, approval_class: str) -> bool:
        if not subject_id:
            return False
        role = self._grants.get(subject_id)
        if role is None:
            return False
        return APPROVER_ROLE_RANK[role] >= APPROVAL_CLASS_RANK.get(approval_class, 99)


__all__ = [
    "ApprovalAuthority",
    "InMemoryApprovalAuthority",
    "ROLE_CRITICAL_APPROVER",
    "ROLE_HIGH_RISK_APPROVER",
    "ROLE_PRIVILEGED_APPROVER",
    "ROLE_STANDARD_APPROVER",
]
