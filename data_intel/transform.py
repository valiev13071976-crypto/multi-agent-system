"""Deterministic execution engine for ``data_intel.nl_ops.OperationPlan``.

Applies a validated, bounded plan to in-memory dataset rows. Every operation
here is one of the explicit, whitelisted shapes produced by the compiler --
there is no arbitrary expression evaluation or model-generated code path
(spec sections 6, 16, 19). All numeric work uses ``Decimal`` so results are
exact and reproducible; string/text/identifier columns are only ever moved,
renamed, or filtered -- never coerced through float (spec section 15).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

from data_intel.cleaning import clean_text, normalize_decimal_string
from data_intel.contracts import ColumnDescriptor
from data_intel.duplicates import find_duplicates
from data_intel.nl_ops import (
    OP_ADD_COLUMN_PERCENT,
    OP_DEDUP,
    OP_FILTER_COMPARE,
    OP_FILTER_CONTAINS,
    OP_LIMIT,
    OP_PERCENT_ROUND,
    OP_REMOVE_COLUMN,
    OP_RENAME_COLUMN,
    OP_SORT,
    OperationPlan,
)


@dataclass
class TransformResult:
    rows: list[dict]
    columns: tuple[ColumnDescriptor, ...]
    row_count_before: int
    row_count_after: int
    applied: list[dict] = field(default_factory=list)
    duplicate_groups: list[dict] = field(default_factory=list)
    # Business-task-ownership/workset-continuation defect closure (PR #90
    # correction): per-row before/after changes, populated only by
    # ``execute_scoped_percent_rules`` below -- empty for every existing
    # ``execute_plan`` caller (a purely additive field, never required).
    row_changes: list[dict] = field(default_factory=list)


def _dec(value: object) -> Decimal | None:
    text = normalize_decimal_string(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _apply_filter_contains(rows: list[dict], params: dict) -> list[dict]:
    column = params["column"]
    needle = (clean_text(params.get("value")) or "").casefold()
    if not needle:
        return rows
    return [r for r in rows if needle in (clean_text(r.get(column)) or "").casefold()]


def _apply_filter_compare(rows: list[dict], params: dict) -> list[dict]:
    column = params["column"]
    op = params["operator"]
    value = Decimal(str(params["value"]))
    out: list[dict] = []
    for r in rows:
        v = _dec(r.get(column))
        if v is None:
            continue
        keep = (
            (op == ">" and v > value)
            or (op == "<" and v < value)
            or (op == ">=" and v >= value)
            or (op == "<=" and v <= value)
        )
        if keep:
            out.append(r)
    return out


def _apply_sort(rows: list[dict], params: dict) -> list[dict]:
    column = params["column"]
    descending = bool(params.get("descending"))

    def key(r: dict):
        v = _dec(r.get(column))
        if v is not None:
            return (0, v)
        return (1, clean_text(r.get(column)) or "")

    return sorted(rows, key=key, reverse=descending)


def _apply_limit(rows: list[dict], params: dict) -> list[dict]:
    n = int(params.get("n") or len(rows))
    return rows[: max(0, n)]


def _round_value(value: Decimal, *, round_mode: str | None, round_to: object) -> Decimal:
    if round_to:
        step = Decimal(str(round_to))
        if step <= 0:
            step = Decimal("1")
        units = value / step
        if round_mode == "up":
            units = units.to_integral_value(rounding=ROUND_CEILING)
        elif round_mode == "down":
            units = units.to_integral_value(rounding=ROUND_FLOOR)
        else:
            units = units.to_integral_value(rounding=ROUND_HALF_UP)
        return units * step
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _apply_percent_round(rows: list[dict], params: dict) -> list[dict]:
    column = params["column"]
    pct = Decimal(str(params["percent"]))
    round_mode = params.get("round_mode")
    round_to = params.get("round_to")
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        v = _dec(row.get(column))
        if v is not None:
            new_v = v * (Decimal("1") + pct / Decimal("100"))
            new_v = _round_value(new_v, round_mode=round_mode, round_to=round_to)
            row[column] = format(new_v, "f")
        out.append(row)
    return out


def _apply_add_column_percent(rows: list[dict], params: dict) -> list[dict]:
    source = params["source_column"]
    new_col = params["new_column"]
    pct = Decimal(str(params["percent"]))
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        v = _dec(row.get(source))
        if v is not None:
            new_v = (v * (Decimal("1") + pct / Decimal("100"))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            row[new_col] = format(new_v, "f")
        else:
            row[new_col] = None
        out.append(row)
    return out


def _apply_remove_column(rows: list[dict], params: dict) -> list[dict]:
    column = params["column"]
    return [{k: v for k, v in r.items() if k != column} for r in rows]


def _apply_rename_column(rows: list[dict], params: dict) -> list[dict]:
    column = params["column"]
    new_name = params["new_name"]
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        if column in row:
            row[new_name] = row.pop(column)
        out.append(row)
    return out


def _update_columns(columns: tuple[ColumnDescriptor, ...], plan: OperationPlan) -> tuple[ColumnDescriptor, ...]:
    cols = list(columns)
    for op in plan.operations:
        if op.op == OP_REMOVE_COLUMN:
            cols = [c for c in cols if c.source_name != op.params["column"]]
        elif op.op == OP_RENAME_COLUMN:
            renamed = []
            for c in cols:
                if c.source_name == op.params["column"]:
                    renamed.append(
                        ColumnDescriptor(
                            source_name=op.params["new_name"],
                            normalized_name=str(op.params["new_name"]).strip().lower(),
                            inferred_type=c.inferred_type,
                            semantic_role=c.semantic_role,
                            confidence=c.confidence,
                            examples_safe=c.examples_safe,
                        )
                    )
                else:
                    renamed.append(c)
            cols = renamed
        elif op.op == OP_ADD_COLUMN_PERCENT:
            cols.append(
                ColumnDescriptor(
                    source_name=op.params["new_column"],
                    normalized_name=str(op.params["new_column"]).strip().lower(),
                    inferred_type="decimal",
                )
            )
    return tuple(cols)


_DISPATCH = {
    OP_FILTER_CONTAINS: _apply_filter_contains,
    OP_FILTER_COMPARE: _apply_filter_compare,
    OP_SORT: _apply_sort,
    OP_LIMIT: _apply_limit,
    OP_PERCENT_ROUND: _apply_percent_round,
    OP_ADD_COLUMN_PERCENT: _apply_add_column_percent,
    OP_REMOVE_COLUMN: _apply_remove_column,
    OP_RENAME_COLUMN: _apply_rename_column,
}


def execute_plan(
    rows: list[dict], columns: tuple[ColumnDescriptor, ...], plan: OperationPlan
) -> TransformResult:
    """Deterministically apply ``plan`` to ``rows``.

    Bounded memory: this operates on the row list the caller already loaded
    (subject to the existing large-dataset/batch routing gate before this is
    ever invoked -- see ``data_intel.planner.assert_sync_data_allowed``).
    """

    current = [dict(r) for r in rows]
    before = len(current)
    applied: list[dict] = []
    duplicate_groups: list[dict] = []

    for op in plan.operations:
        if op.op == OP_DEDUP:
            groups = find_duplicates(current)
            duplicate_groups = groups
            if op.params.get("remove"):
                drop_indices: set[int] = set()
                for g in groups:
                    idxs = list(g.get("indices") or [])
                    drop_indices.update(idxs[1:])
                current = [r for i, r in enumerate(current) if i not in drop_indices]
        else:
            handler = _DISPATCH.get(op.op)
            if handler is None:
                continue
            current = handler(current, op.params)
        applied.append({"op": op.op, "params": dict(op.params)})

    new_columns = _update_columns(columns, plan)
    return TransformResult(
        rows=current,
        columns=new_columns,
        row_count_before=before,
        row_count_after=len(current),
        applied=applied,
        duplicate_groups=duplicate_groups,
    )


# ---------------------------------------------------------------------------
# Compound scoped-rule executor (business-task-ownership/workset-
# continuation defect closure, PR #90 correction).
#
# A COMPOUND request assigns two or more DIFFERENT transformations to two
# or more DIFFERENT, non-overlapping row scopes in the SAME turn -- e.g.
# "first three rows +7%, the rest +15%". ``OperationPlan``/``execute_plan``
# above only ever describe ONE flat sequence of operations applied to
# WHATEVER rows survive each filter in turn; they cannot express "subset A
# gets transform A, subset B gets transform B" in one shape. Rather than
# inventing a second execution engine, this reuses the EXACT SAME
# validated, whitelisted primitives (percent-round arithmetic, text-
# contains matching, numeric comparison -- see ``_dec``/``_round_value``/
# ``clean_text`` above) per scope, plus one new scope kind
# (``row_position_range``) for "first N"/"rows K..M" requests that has no
# equivalent in ``OperationPlan`` at all (a POSITION range, not a column
# filter).
#
# Callers MUST have already validated ``rules`` against the real table
# schema (see ``data_intel.nl_ops.validate_scoped_price_rules``) -- this
# function trusts its input completely, exactly like every other function
# in this module trusts an already-validated ``OperationPlan``.
# ---------------------------------------------------------------------------


def _match_row_position_range(row_count: int, *, start_position, end_position) -> set[int]:
    start = max(1, int(start_position or 1))
    end = int(end_position) if end_position not in (None, "") else row_count
    end = min(end, row_count)
    if end < start:
        return set()
    return set(range(start - 1, end))


def _match_text_contains(rows: list[dict], *, column: str, contains: str) -> set[int]:
    needle = (clean_text(contains) or "").casefold()
    if not needle:
        return set()
    return {i for i, r in enumerate(rows) if needle in (clean_text(r.get(column)) or "").casefold()}


def _match_price_compare(rows: list[dict], *, column: str, operator: str, threshold) -> set[int]:
    if threshold in (None, ""):
        return set()
    value = Decimal(str(threshold))
    out: set[int] = set()
    for i, r in enumerate(rows):
        v = _dec(r.get(column))
        if v is None:
            continue
        keep = (
            (operator == "gt" and v > value)
            or (operator == "gte" and v >= value)
            or (operator == "lt" and v < value)
            or (operator == "lte" and v <= value)
            or (operator == "eq" and v == value)
        )
        if keep:
            out.add(i)
    return out


def _match_scope(rows: list[dict], scope: dict) -> set[int]:
    kind = scope.get("kind")
    if kind == "row_position_range":
        return _match_row_position_range(
            len(rows), start_position=scope.get("start_position"), end_position=scope.get("end_position")
        )
    if kind == "text_contains":
        return _match_text_contains(rows, column=scope["column"], contains=scope.get("contains"))
    if kind == "price_compare":
        return _match_price_compare(
            rows, column=scope["column"], operator=scope.get("operator") or "gte", threshold=scope.get("threshold")
        )
    if kind == "remainder":
        return set(range(len(rows)))
    return set()


def execute_scoped_percent_rules(
    rows: list[dict], columns: tuple[ColumnDescriptor, ...], rules: list[dict]
) -> TransformResult:
    """Deterministically applies a COMPOUND list of percent-adjustment
    ``rules``, each scoped to a DIFFERENT, non-overlapping subset of
    ``rows``, in ONE execution -- the counterpart to ``execute_plan`` for a
    shape ``OperationPlan`` cannot express (see module note above).

    Every ``rule`` is ``{"scope": {...}, "column": <price column
    source_name>, "percent": <signed percent string/number>}``. Rules are
    applied IN ORDER; a rule's scope only ever matches rows NOT ALREADY
    claimed by an earlier rule in this SAME call, so a ``"remainder"``
    scope (or any scope that happens to overlap an earlier one) can never
    double-apply a change to the same row. Rows never claimed by any rule
    are returned completely unchanged -- this function never requires full
    coverage.

    Returns a ``TransformResult`` whose ``row_changes`` records exactly
    which row/column changed from what value to what value, for which
    rule -- the deterministic source of every number a caller-facing
    preview may show (never re-derived from free text or a model's own
    paraphrase)."""

    current = [dict(r) for r in rows]
    claimed: set[int] = set()
    applied: list[dict] = []
    row_changes: list[dict] = []

    for rule_index, rule in enumerate(rules):
        scope = dict(rule.get("scope") or {})
        column = rule["column"]
        percent = Decimal(str(rule["percent"]))
        matched = _match_scope(current, scope) - claimed
        for idx in sorted(matched):
            row = current[idx]
            before = _dec(row.get(column))
            if before is None:
                continue
            after = _round_value(before * (Decimal("1") + percent / Decimal("100")), round_mode=None, round_to=None)
            row[column] = format(after, "f")
            row_changes.append(
                {
                    "row_index": idx,
                    "column": column,
                    "before": format(before, "f"),
                    "after": format(after, "f"),
                    "percent": format(percent, "f"),
                    "rule_index": rule_index,
                }
            )
        claimed |= matched
        applied.append(
            {
                "op": "scoped_percent_rule",
                "params": {"scope": scope, "column": column, "percent": format(percent, "f"), "matched_rows": len(matched)},
            }
        )

    return TransformResult(
        rows=current,
        columns=columns,
        row_count_before=len(rows),
        row_count_after=len(current),
        applied=applied,
        duplicate_groups=[],
        row_changes=row_changes,
    )
