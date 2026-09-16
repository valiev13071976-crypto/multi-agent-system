"""CANONICAL TABLE EXECUTION -- ONE-SHOT model-call NL -> structured
``OperationPlan`` compiler (Block 5.1 follow-up, PR #92 correction).

This is the PRIMARY semantic boundary for new canonical table execution
(``business_assistant.conversation_gateway._maybe_execute_canonical_table_
operation``), replacing ``data_intel.nl_ops.compile_request`` (a bounded
deterministic RU/EN regex/stem compiler) in that role. ``compile_request``
itself is UNCHANGED and keeps serving its own, pre-existing callers
(``data_intel.service.DataIntelligenceService.execute_nl_request``) for
backward compatibility -- it is never invoked from here.

Architecture (exactly one model call, never a loop):

    user text + table schema (columns, row_count)
        |
        v
    ONE model semantic call (``model_call``, default: the EXISTING
    ``agents.openai_agent.OpenAIAgent.run`` one-shot HTTP call already
    configured for this deployment -- the SAME ``OPENAI_API_KEY``/
    ``OPENAI_MODEL`` env vars ``managed_agent_poc`` already requires; no
    new provider, no agent loop, no tool-calling round trips)
        |
        v
    strict JSON text -> parsed -> DETERMINISTICALLY validated against
    THIS table's actual columns/row count (never trusted blindly)
        |
        v
    validated ``data_intel.nl_ops.OperationPlan`` (with per-rule
    ``OperationScope`` -- scope A -> op A, remainder -> op B, ...)
        |
        v
    (caller) ``data_intel.transform.execute_plan`` -- the SAME existing
    deterministic executor every other table operation already uses.

The model interprets language ONLY: it returns text, never runs code,
never touches a dataset/session/store, and is called exactly once per
turn -- there is no second interpretation of ``text`` after this
function returns (or raises ``ModelPlanNotApplicable``).
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from data_intel.contracts import TableDescriptor
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
    SCOPE_ALL,
    SCOPE_REMAINDER,
    SCOPE_ROW_RANGE,
    OperationPlan,
    OperationScope,
    PlannedOperation,
)

ModelCall = Callable[[str], Awaitable[str]]

_ALLOWED_OPS = (
    OP_PERCENT_ROUND,
    OP_ADD_COLUMN_PERCENT,
    OP_FILTER_CONTAINS,
    OP_FILTER_COMPARE,
    OP_SORT,
    OP_LIMIT,
    OP_REMOVE_COLUMN,
    OP_RENAME_COLUMN,
    OP_DEDUP,
)
_COLUMN_REQUIRED_OPS = (
    OP_PERCENT_ROUND,
    OP_ADD_COLUMN_PERCENT,
    OP_FILTER_CONTAINS,
    OP_FILTER_COMPARE,
    OP_SORT,
    OP_REMOVE_COLUMN,
    OP_RENAME_COLUMN,
)
_SCOPE_KINDS = (SCOPE_ALL, SCOPE_ROW_RANGE, SCOPE_REMAINDER)


class ModelPlanNotApplicable(Exception):
    """Raised for EVERY outcome other than "the model produced a strictly
    valid, executable table-wide plan": the model itself decided this text
    is not a table-wide operation (``applicable: false`` -- e.g. a plain
    analysis, a single-product selection, a write-plan question), a
    network/provider failure, malformed JSON, or a plan that failed
    deterministic validation against THIS table's actual schema/row
    count. The caller must defer to the existing managed-agent/legacy
    routing for this turn -- never guess, never partially execute."""


def _default_model_call() -> ModelCall:
    """The SMALLEST EXISTING one-shot model-call seam in this repository:
    ``agents.openai_agent.OpenAIAgent.run`` -- a single ``POST /v1/responses``
    HTTP call (see ``agents/openai_agent.py``), already configured with the
    SAME ``OPENAI_API_KEY``/``OPENAI_MODEL`` environment variables
    ``managed_agent_poc`` requires for this exact deployment. No agent
    loop, no tool-calling, no session/state -- text in, text out."""

    from agents.openai_agent import OpenAIAgent

    agent = OpenAIAgent()

    async def _call(prompt: str) -> str:
        result = await agent.run(prompt)
        return result.text

    return _call


def _build_prompt(text: str, table: TableDescriptor, row_count: int) -> str:
    columns = ", ".join(c.source_name for c in table.columns)
    return (
        "You translate ONE user request about a data table into STRICT JSON. "
        "Respond with ONLY a single JSON object -- no prose, no markdown code fences, "
        "no explanation before or after it.\n\n"
        f"Table columns (use these EXACT names, nothing else): {columns}\n"
        f"Current row count: {row_count}\n\n"
        "Output JSON schema:\n"
        "{\n"
        '  "applicable": <bool>,\n'
        '  "wants_workbook": <bool>,\n'
        '  "operations": [\n'
        "    {\n"
        '      "scope": {"kind": "all" | "row_range" | "remainder", "start": <int, only for row_range>, "end": <int, only for row_range>},\n'
        '      "column": <string, one of the table columns above>,\n'
        '      "operation": "percent_round" | "add_column_percent" | "filter_contains" | "filter_compare" | "sort" | "limit" | "remove_column" | "rename_column" | "dedup",\n'
        '      "value": <string or number, meaning depends on "operation" -- e.g. the signed percent for percent_round/add_column_percent, the comparison operator threshold for filter_compare, the substring for filter_contains, the row count for limit>,\n'
        '      "operator": <one of ">"|"<"|">="|"<=" -- ONLY for filter_compare>,\n'
        '      "new_column": <string -- ONLY for add_column_percent/rename_column>,\n'
        '      "descending": <bool -- ONLY for sort>\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        'Set "applicable" to false (and "operations" to an empty list) when the user is NOT asking to '
        "transform/filter/sort/deduplicate this WHOLE table -- for example: analyzing/summarizing the table, "
        "selecting or inspecting one specific product/row, preparing a product card, or asking what would be "
        "written to an external system. Only set it to true for a genuine table-wide (or table-subset) "
        "mutation/filter/sort/dedup request.\n"
        'Use "row_range" with a 0-based, half-open [start, end) row-index window for an explicit ordinal subset '
        '(e.g. "the first 3 rows" is start=0, end=3). Use "remainder" for every row NOT covered by an earlier rule '
        'in THIS SAME response -- you may emit several rules, each with its own scope, to express a compound '
        'request such as "the first N rows get X, everyone else gets Y". Use "all" when a rule applies to the '
        "whole table.\n"
        "Never invent a column name that is not in the list above, never invent row values, and never describe or "
        "produce any code -- only the operations listed above, applied by an existing deterministic engine.\n\n"
        f"User request: {text}"
    )


def _parse_json_object(raw_text: str) -> Any:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        stripped = text.lstrip()
        if stripped[:4].lower() == "json":
            text = stripped[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _validate_decimal(value: Any) -> Decimal:
    try:
        cleaned = str(value).strip().replace(",", ".").replace("%", "").replace(" ", "")
        return Decimal(cleaned)
    except (InvalidOperation, AttributeError, TypeError):
        raise ModelPlanNotApplicable(f"invalid_numeric_value:{value!r}") from None


def _validate_scope(raw_scope: Any, *, row_count: int) -> OperationScope:
    if raw_scope is None:
        return OperationScope(kind=SCOPE_ALL)
    if not isinstance(raw_scope, dict):
        raise ModelPlanNotApplicable("malformed_scope")
    kind = str(raw_scope.get("kind") or SCOPE_ALL)
    if kind not in _SCOPE_KINDS:
        raise ModelPlanNotApplicable(f"unknown_scope_kind:{kind}")
    if kind in (SCOPE_ALL, SCOPE_REMAINDER):
        return OperationScope(kind=kind)
    try:
        start = int(raw_scope.get("start"))
        end = int(raw_scope.get("end"))
    except (TypeError, ValueError):
        raise ModelPlanNotApplicable("invalid_row_range") from None
    if start < 0 or end < start or end > row_count:
        raise ModelPlanNotApplicable(f"row_range_out_of_bounds:{start}-{end}/{row_count}")
    return OperationScope(kind=SCOPE_ROW_RANGE, start=start, end=end)


def _validate_operation(raw_op: Any, *, valid_columns: set[str], row_count: int) -> PlannedOperation:
    if not isinstance(raw_op, dict):
        raise ModelPlanNotApplicable("malformed_operation")
    op_name = str(raw_op.get("operation") or "")
    if op_name not in _ALLOWED_OPS:
        raise ModelPlanNotApplicable(f"disallowed_operation:{op_name}")
    scope = _validate_scope(raw_op.get("scope"), row_count=row_count)

    column = raw_op.get("column")
    if op_name in _COLUMN_REQUIRED_OPS:
        if not isinstance(column, str) or column not in valid_columns:
            raise ModelPlanNotApplicable(f"unknown_column:{column!r}")

    value = raw_op.get("value")
    if op_name == OP_PERCENT_ROUND:
        pct = _validate_decimal(value)
        params = {"column": column, "percent": str(pct), "round_mode": None, "round_to": None}
    elif op_name == OP_ADD_COLUMN_PERCENT:
        pct = _validate_decimal(value)
        new_column = raw_op.get("new_column")
        if not isinstance(new_column, str) or not new_column.strip():
            raise ModelPlanNotApplicable("missing_new_column")
        params = {"source_column": column, "new_column": new_column.strip(), "percent": str(pct)}
    elif op_name == OP_FILTER_CONTAINS:
        params = {"column": column, "value": str(value if value is not None else "")}
    elif op_name == OP_FILTER_COMPARE:
        operator = str(raw_op.get("operator") or "")
        if operator not in (">", "<", ">=", "<="):
            raise ModelPlanNotApplicable(f"invalid_operator:{operator!r}")
        threshold = _validate_decimal(value)
        params = {"column": column, "operator": operator, "value": str(threshold)}
    elif op_name == OP_SORT:
        params = {"column": column, "descending": bool(raw_op.get("descending"))}
    elif op_name == OP_LIMIT:
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ModelPlanNotApplicable(f"invalid_limit:{value!r}") from None
        if n < 0:
            raise ModelPlanNotApplicable(f"invalid_limit:{value!r}")
        params = {"n": n}
    elif op_name == OP_REMOVE_COLUMN:
        params = {"column": column}
    elif op_name == OP_RENAME_COLUMN:
        new_name = raw_op.get("new_column") or value
        if not isinstance(new_name, str) or not new_name.strip():
            raise ModelPlanNotApplicable("missing_new_name")
        params = {"column": column, "new_name": new_name.strip()}
    elif op_name == OP_DEDUP:
        params = {"remove": bool(value)}
    else:  # pragma: no cover -- unreachable, _ALLOWED_OPS already checked above
        raise ModelPlanNotApplicable(f"unsupported_operation:{op_name}")

    return PlannedOperation(op_name, params, scope=scope)


async def compile_request_via_model(
    text: str,
    table: TableDescriptor,
    row_count: int,
    *,
    model_call: ModelCall | None = None,
) -> OperationPlan:
    """Make ONE model call to interpret ``text`` against ``table``'s own
    schema, then DETERMINISTICALLY parse and validate its JSON response
    into a validated ``OperationPlan`` -- never trusting the model's
    output as-is. Raises ``ModelPlanNotApplicable`` for every non-genuine-
    table-operation outcome (the model's own "applicable: false" judgment,
    a provider/parse failure, or a plan that fails schema/column/numeric
    validation against THIS table); the caller must treat that exactly
    like ``compile_request``'s own ``UnsupportedOperationError``/
    ``AmbiguousOperationError`` -- defer, never guess, never partially
    execute."""

    prompt = _build_prompt(text, table, row_count)
    try:
        caller = model_call or _default_model_call()
        raw_text = await caller(prompt)
    except Exception as exc:
        raise ModelPlanNotApplicable(f"model_call_failed:{exc}") from None

    payload = _parse_json_object(raw_text)
    if not isinstance(payload, dict):
        raise ModelPlanNotApplicable("model_output_not_json")
    if not payload.get("applicable"):
        raise ModelPlanNotApplicable("model_marked_not_applicable")

    raw_ops = payload.get("operations")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise ModelPlanNotApplicable("no_operations")

    valid_columns = {c.source_name for c in table.columns}
    operations = tuple(
        _validate_operation(raw_op, valid_columns=valid_columns, row_count=row_count) for raw_op in raw_ops
    )

    return OperationPlan(
        operations=operations,
        wants_workbook=bool(payload.get("wants_workbook")),
        raw_text=text,
        intent_summary=", ".join(op.op for op in operations),
    )
