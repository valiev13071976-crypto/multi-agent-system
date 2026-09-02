"""Declarative bounded condition evaluation."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from controlled_automation.config import MAX_CONDITION_DEPTH
from controlled_automation.errors import CONDITION_PARTIAL, CONDITION_STALE, CONDITION_UNKNOWN, ControlledAutomationError
from controlled_automation.models import DATA_ERROR, DATA_KNOWN, DATA_PARTIAL, DATA_STALE, DATA_UNKNOWN

OPERATORS = frozenset({"EQ", "NE", "GT", "GTE", "LT", "LTE", "IN", "NOT_IN", "EXISTS", "CHANGED", "PERCENT_CHANGE_GT", "PERCENT_CHANGE_LT"})


def _quality(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("_quality") or DATA_KNOWN)
    return DATA_KNOWN


def _raw(value: Any) -> Any:
    if isinstance(value, dict) and "_value" in value:
        return value["_value"]
    return value


def _as_decimal(value: Any) -> Decimal | None:
    try:
        if value is None:
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def evaluate_condition(node: dict[str, Any], *, facts: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    if depth > MAX_CONDITION_DEPTH:
        return {"satisfied": False, "quality": DATA_ERROR, "reason": "max_depth"}

    op = str(node.get("op") or node.get("logic") or "").upper()

    if op in {"ALL", "ANY"}:
        children = list(node.get("conditions") or [])
        if not children:
            return {"satisfied": False, "quality": DATA_ERROR, "reason": "empty_group"}
        results = [evaluate_condition(c, facts=facts, depth=depth + 1) for c in children]
        qualities = {r["quality"] for r in results}
        if DATA_ERROR in qualities:
            return {"satisfied": False, "quality": DATA_ERROR, "reason": "child_error"}
        if DATA_UNKNOWN in qualities:
            return {"satisfied": False, "quality": DATA_UNKNOWN, "reason": "unknown_data"}
        if DATA_STALE in qualities:
            return {"satisfied": False, "quality": DATA_STALE, "reason": "stale_data"}
        if DATA_PARTIAL in qualities:
            return {"satisfied": False, "quality": DATA_PARTIAL, "reason": "partial_data"}
        satisfied = all(r["satisfied"] for r in results) if op == "ALL" else any(r["satisfied"] for r in results)
        return {"satisfied": satisfied, "quality": DATA_KNOWN, "results": results}

    field = str(node.get("field") or "")
    operator = str(node.get("operator") or node.get("op") or "EQ").upper()
    if operator not in OPERATORS:
        raise ControlledAutomationError(CONDITION_UNKNOWN, f"operator:{operator}")

    left = facts.get(field)
    quality = _quality(left)
    if quality == DATA_UNKNOWN:
        return {"satisfied": False, "quality": DATA_UNKNOWN, "field": field}
    if quality == DATA_STALE:
        return {"satisfied": False, "quality": DATA_STALE, "field": field}
    if quality == DATA_PARTIAL:
        return {"satisfied": False, "quality": DATA_PARTIAL, "field": field}
    if quality == DATA_ERROR:
        return {"satisfied": False, "quality": DATA_ERROR, "field": field}

    right = node.get("value")
    lv = _raw(left)
    rv = _raw(right)

    if operator == "EXISTS":
        return {"satisfied": lv is not None, "quality": DATA_KNOWN, "field": field}
    if operator == "EQ":
        return {"satisfied": lv == rv, "quality": DATA_KNOWN, "field": field}
    if operator == "NE":
        return {"satisfied": lv != rv, "quality": DATA_KNOWN, "field": field}
    if operator in {"GT", "GTE", "LT", "LTE"}:
        ld, rd = _as_decimal(lv), _as_decimal(rv)
        if ld is None or rd is None:
            return {"satisfied": False, "quality": DATA_UNKNOWN, "field": field}
        if operator == "GT":
            ok = ld > rd
        elif operator == "GTE":
            ok = ld >= rd
        elif operator == "LT":
            ok = ld < rd
        else:
            ok = ld <= rd
        return {"satisfied": ok, "quality": DATA_KNOWN, "field": field}
    if operator == "IN":
        opts = set(rv or [])
        return {"satisfied": lv in opts, "quality": DATA_KNOWN, "field": field}
    if operator == "NOT_IN":
        opts = set(rv or [])
        return {"satisfied": lv not in opts, "quality": DATA_KNOWN, "field": field}
    if operator == "CHANGED":
        prev = facts.get(f"{field}_previous")
        if _quality(prev) != DATA_KNOWN:
            return {"satisfied": False, "quality": DATA_UNKNOWN, "field": field}
        return {"satisfied": _raw(prev) != lv, "quality": DATA_KNOWN, "field": field}
    if operator in {"PERCENT_CHANGE_GT", "PERCENT_CHANGE_LT"}:
        prev = facts.get(f"{field}_previous")
        if _quality(prev) != DATA_KNOWN:
            return {"satisfied": False, "quality": DATA_UNKNOWN, "field": field}
        ld, pd = _as_decimal(lv), _as_decimal(_raw(prev))
        if ld is None or pd is None or pd == 0:
            return {"satisfied": False, "quality": DATA_UNKNOWN, "field": field}
        pct = ((ld - pd) / abs(pd)) * 100
        threshold = _as_decimal(rv) or Decimal("0")
        ok = pct > threshold if operator == "PERCENT_CHANGE_GT" else pct < threshold
        return {"satisfied": ok, "quality": DATA_KNOWN, "field": field, "pct": str(pct)}

    return {"satisfied": False, "quality": DATA_ERROR, "reason": "unsupported"}
