"""Structured branching — no arbitrary code/expression execution."""

from __future__ import annotations

from typing import Mapping

from workflow.definition import (
    BRANCH_OP_EQ,
    BRANCH_OP_EXISTS,
    BRANCH_OP_FALSY,
    BRANCH_OP_IN,
    BRANCH_OP_NE,
    BRANCH_OP_TRUTHY,
    BranchCondition,
    BranchRule,
)


def _resolve_field(data: Mapping[str, object], field: str) -> object:
    if not field:
        return data
    cur: object = data
    for part in str(field).split("."):
        if not isinstance(cur, Mapping):
            return None
        if part not in cur:
            return None
        cur = cur[part]
    return cur


def evaluate_condition(
    condition: BranchCondition,
    step_results: Mapping[str, Mapping[str, object]],
) -> bool:
    source = step_results.get(condition.source_step_id) or {}
    value = _resolve_field(source, condition.field)
    op = condition.op
    if op == BRANCH_OP_EXISTS:
        return value is not None
    if op == BRANCH_OP_TRUTHY:
        return bool(value)
    if op == BRANCH_OP_FALSY:
        return not bool(value)
    if op == BRANCH_OP_EQ:
        return value == condition.value
    if op == BRANCH_OP_NE:
        return value != condition.value
    if op == BRANCH_OP_IN:
        expected = condition.value
        if isinstance(expected, (list, tuple, set, frozenset)):
            return value in expected
        return False
    return False


def resolve_branch(
    rule: BranchRule,
    step_results: Mapping[str, Mapping[str, object]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (activate_steps, skip_steps)."""

    matched = evaluate_condition(rule.condition, step_results)
    if matched:
        return rule.then_steps, rule.else_steps
    return rule.else_steps, rule.then_steps
