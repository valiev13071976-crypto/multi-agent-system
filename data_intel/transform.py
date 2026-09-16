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
from data_intel.contracts import (
    ROLE_PURCHASE_PRICE,
    ROLE_SELLING_PRICE,
    ROLE_UNKNOWN,
    ColumnDescriptor,
)
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
    OperationScope,
    SCOPE_REMAINDER,
    SCOPE_ROW_RANGE,
)

# CANONICAL TABLE EXECUTION (compound scoped operations): only these two
# row-VALUE-mutating operations have per-row scope semantics -- every
# other operation (filter/sort/limit/dedup/rename/remove-column) is
# already inherently table-wide/structural and keeps its existing,
# unscoped behavior regardless of whatever ``scope`` a ``PlannedOperation``
# happens to carry (compile_request never sets one, and the model-call
# compiler never attaches one to these either -- see
# ``data_intel.nl_plan_llm``).
_SCOPED_ROW_OPS = (OP_PERCENT_ROUND, OP_ADD_COLUMN_PERCENT)


def _resolve_scope_indices(scope: OperationScope, *, total: int, covered: set[int]) -> set[int]:
    """Resolve ``scope`` against the CURRENT row count (i.e. after any
    earlier filter/sort/dedup in the SAME plan already ran) into the
    concrete 0-based row indices a scoped operation applies to this call.
    ``covered`` is the running union of indices already touched by an
    earlier SCOPED rule in this SAME ``execute_plan`` invocation -- the
    ONLY state ``SCOPE_REMAINDER`` needs, resolved here deterministically,
    never guessed by whichever compiler produced the plan."""
    kind = getattr(scope, "kind", None)
    if kind == SCOPE_ROW_RANGE:
        start = max(0, int(scope.start or 0))
        end = min(total, int(scope.end) if scope.end is not None else total)
        return set(range(start, max(start, end)))
    if kind == SCOPE_REMAINDER:
        return set(range(total)) - covered
    return set(range(total))


def _scope_dict(scope: OperationScope) -> dict:
    return {"kind": scope.kind, "start": scope.start, "end": scope.end}


@dataclass
class TransformResult:
    rows: list[dict]
    columns: tuple[ColumnDescriptor, ...]
    row_count_before: int
    row_count_after: int
    applied: list[dict] = field(default_factory=list)
    duplicate_groups: list[dict] = field(default_factory=list)
    # CANONICAL TABLE EXECUTION (compound scoped operations): the executor's
    # OWN record of exactly which row/column each rule's ``scope`` actually
    # touched, captured at mutation time -- never reconstructed afterwards
    # by inverting a specific operation's formula. Empty for every plan
    # with no scoped row-mutating operation (unaffected, zero behavior
    # change for every existing caller of ``execute_plan``).
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


def _apply_percent_round(rows: list[dict], params: dict, indices: set[int] | None = None) -> list[dict]:
    column = params["column"]
    pct = Decimal(str(params["percent"]))
    round_mode = params.get("round_mode")
    round_to = params.get("round_to")
    out: list[dict] = []
    for i, r in enumerate(rows):
        row = dict(r)
        if indices is None or i in indices:
            v = _dec(row.get(column))
            if v is not None:
                new_v = v * (Decimal("1") + pct / Decimal("100"))
                new_v = _round_value(new_v, round_mode=round_mode, round_to=round_to)
                row[column] = format(new_v, "f")
        out.append(row)
    return out


def _apply_add_column_percent(rows: list[dict], params: dict, indices: set[int] | None = None) -> list[dict]:
    source = params["source_column"]
    new_col = params["new_column"]
    pct = Decimal(str(params["percent"]))
    out: list[dict] = []
    for i, r in enumerate(rows):
        row = dict(r)
        if indices is None or i in indices:
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
            # Retail-vs-purchase price separation (production defect
            # closure): a derived column explicitly declared as the RETAIL/
            # SELLING price (``price_role`` -- set deterministically by
            # ``data_intel.nl_plan_llm`` from the model's own semantic
            # judgment, never guessed here) is tagged with
            # ``ROLE_SELLING_PRICE`` so every downstream consumer that
            # already distinguishes purchase vs. retail by role (``data_
            # intel.service._row_lookup_result``'s card/preview, Bitrix
            # readiness) picks it up as the retail price -- never as an
            # unrelated/unknown column, and never confused with the
            # untouched source purchase-price column. Symmetric for an
            # explicit "purchase" role. Absent/unrecognized ``price_role``
            # keeps the prior, unchanged behavior (``ROLE_UNKNOWN``).
            result_role = ROLE_UNKNOWN
            price_role = op.params.get("price_role")
            if price_role == "retail":
                result_role = ROLE_SELLING_PRICE
            elif price_role == "purchase":
                result_role = ROLE_PURCHASE_PRICE
            cols.append(
                ColumnDescriptor(
                    source_name=op.params["new_column"],
                    normalized_name=str(op.params["new_column"]).strip().lower(),
                    inferred_type="decimal",
                    semantic_role=result_role,
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
    row_changes: list[dict] = []
    # CANONICAL TABLE EXECUTION (compound scoped operations): the running
    # union of row indices already touched by an earlier SCOPED rule in
    # THIS plan -- the only state ``SCOPE_REMAINDER`` needs (see
    # ``_resolve_scope_indices``). Reset per ``execute_plan`` call, never
    # persisted -- a later, unrelated plan starts with an empty set.
    covered_indices: set[int] = set()

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
        elif op.op in _SCOPED_ROW_OPS:
            indices = _resolve_scope_indices(op.scope, total=len(current), covered=covered_indices)
            handler = _DISPATCH[op.op]
            before_rows = {i: dict(current[i]) for i in indices if i < len(current)}
            current = handler(current, op.params, indices)
            column = op.params.get("column") or op.params.get("source_column")
            result_column = op.params.get("new_column") or column
            for i in sorted(before_rows):
                if i >= len(current):
                    continue
                before_value = before_rows[i].get(column) if column else None
                after_value = current[i].get(result_column) if result_column else None
                row_changes.append(
                    {
                        "row_index": i,
                        "column": result_column,
                        "operation": op.op,
                        "params": dict(op.params),
                        "scope": _scope_dict(op.scope),
                        "before": before_value,
                        "after": after_value,
                        "row_after": dict(current[i]),
                    }
                )
            covered_indices |= indices
        else:
            handler = _DISPATCH.get(op.op)
            if handler is None:
                continue
            current = handler(current, op.params)
        applied.append({"op": op.op, "params": dict(op.params), "scope": _scope_dict(op.scope)})

    new_columns = _update_columns(columns, plan)
    return TransformResult(
        rows=current,
        columns=new_columns,
        row_count_before=before,
        row_count_after=len(current),
        applied=applied,
        duplicate_groups=duplicate_groups,
        row_changes=row_changes,
    )
