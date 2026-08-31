"""Canonical side-effect semantic levels for Tool Gateway policy."""

from __future__ import annotations

from tools.errors import ToolPolicyDeniedError
from tools.models import (
    APPROVAL_POLICY_NONE,
    OP_DESTRUCTIVE,
    OP_PRIVILEGED,
    OP_READ,
    OP_WRITE,
    SIDE_EFFECT_CRITICAL,
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_READ,
    SIDE_EFFECT_WRITE,
    TOOL_TRUST_PRIVILEGED,
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    ToolDescriptor,
)

# Canonical semantic levels (governance-facing)
SEMANTIC_READ_ONLY = "READ_ONLY"
SEMANTIC_WRITE_REVERSIBLE = "WRITE_REVERSIBLE"
SEMANTIC_WRITE_EXTERNAL = "WRITE_EXTERNAL"
SEMANTIC_DESTRUCTIVE = "DESTRUCTIVE"
SEMANTIC_FINANCIAL_OR_HIGH_RISK = "FINANCIAL_OR_HIGH_RISK"

SEMANTIC_SIDE_EFFECT_LEVELS = (
    SEMANTIC_READ_ONLY,
    SEMANTIC_WRITE_REVERSIBLE,
    SEMANTIC_WRITE_EXTERNAL,
    SEMANTIC_DESTRUCTIVE,
    SEMANTIC_FINANCIAL_OR_HIGH_RISK,
)

_HIGH_RISK_SEMANTICS = frozenset({SEMANTIC_DESTRUCTIVE, SEMANTIC_FINANCIAL_OR_HIGH_RISK})


def resolve_semantic_side_effect(descriptor: ToolDescriptor, operation: str = "") -> str:
    """Map ToolDescriptor fields → canonical semantic side-effect level."""
    meta = dict(descriptor.metadata or {})
    if meta.get("financial_risk") or meta.get("financial_or_high_risk"):
        return SEMANTIC_FINANCIAL_OR_HIGH_RISK
    if descriptor.trust_level == TOOL_TRUST_PRIVILEGED:
        return SEMANTIC_FINANCIAL_OR_HIGH_RISK
    op_class = descriptor.operation_class_for(operation or (descriptor.operations[0] if descriptor.operations else ""))
    if op_class in {OP_DESTRUCTIVE, OP_PRIVILEGED}:
        return SEMANTIC_DESTRUCTIVE
    if descriptor.side_effect_level == SIDE_EFFECT_CRITICAL:
        return SEMANTIC_DESTRUCTIVE
    if descriptor.read_only or descriptor.side_effect_level == SIDE_EFFECT_READ:
        return SEMANTIC_READ_ONLY
    if descriptor.side_effect_level == SIDE_EFFECT_WRITE:
        return SEMANTIC_WRITE_EXTERNAL
    if descriptor.reversible and descriptor.trust_level != TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE:
        return SEMANTIC_WRITE_REVERSIBLE
    if not descriptor.read_only:
        return SEMANTIC_WRITE_EXTERNAL if not descriptor.reversible else SEMANTIC_WRITE_REVERSIBLE
    return SEMANTIC_READ_ONLY


def enforce_side_effect_policy(descriptor: ToolDescriptor, operation: str) -> None:
    """Fail-closed side-effect policy before adapter execution."""
    semantic = resolve_semantic_side_effect(descriptor, operation)
    op_class = descriptor.operation_class_for(operation)
    if descriptor.read_only and op_class not in {OP_READ, ""}:
        raise ToolPolicyDeniedError("forbidden_scope")
    if semantic in _HIGH_RISK_SEMANTICS and descriptor.approval_policy == APPROVAL_POLICY_NONE:
        raise ToolPolicyDeniedError("tool_policy_denied")
    if semantic == SEMANTIC_DESTRUCTIVE and descriptor.read_only:
        raise ToolPolicyDeniedError("destructive_read_only_conflict")
