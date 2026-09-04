"""Publication eligibility from ProductContentPackage — no governance bypass."""

from __future__ import annotations

from product_content.contracts import (
    STATUS_BLOCKED,
    STATUS_READY,
    STATUS_READY_WITH_WARNINGS,
    STATUS_REQUIRES_REVIEW,
    ProductContentPackage,
)

from governed_publish.contracts import STATUS_APPROVAL_REQUIRED, STATUS_BLOCKED as PUB_BLOCKED


def eligibility_for(package: ProductContentPackage, *, warnings_require_review: bool = True) -> tuple[str, str]:
    """Return (publish_gate, reason). Preview is always allowed; execute follows gate."""
    if package.status == STATUS_BLOCKED or any("unsupported_claim" in i for i in package.issues):
        return PUB_BLOCKED, "content_blocked"
    if package.status == STATUS_REQUIRES_REVIEW:
        return PUB_BLOCKED, "requires_review_no_auto_publish"
    if package.status == STATUS_READY_WITH_WARNINGS and warnings_require_review:
        return STATUS_APPROVAL_REQUIRED, "warnings_require_review"
    if package.status == STATUS_READY:
        return STATUS_APPROVAL_REQUIRED, "write_requires_hitl"
    if package.status == STATUS_READY_WITH_WARNINGS:
        return STATUS_APPROVAL_REQUIRED, "warnings_hitl"
    return PUB_BLOCKED, f"unknown_status:{package.status}"


def can_preview(package: ProductContentPackage) -> bool:
    return True


def can_plan(package: ProductContentPackage) -> bool:
    gate, _ = eligibility_for(package)
    return gate != PUB_BLOCKED
