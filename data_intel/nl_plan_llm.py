"""CANONICAL TABLE EXECUTION -- ONE-SHOT model-call NL -> structured
``OperationPlan`` compiler (Block 5.1 follow-up, PR #92; PR #93 defect
closure).

This is the PRIMARY semantic boundary for new canonical table execution
(``business_assistant.conversation_gateway._maybe_execute_canonical_table_
operation``), replacing ``data_intel.nl_ops.compile_request`` (a bounded
deterministic RU/EN regex/stem compiler) in that role. ``compile_request``
itself is UNCHANGED and keeps serving its own, pre-existing callers
(``data_intel.service.DataIntelligenceService.execute_nl_request``) for
backward compatibility -- it is never invoked from here.

Architecture (exactly one model call, never a loop):

    user text + table schema (stable column IDs, never raw column text)
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
    THIS table's actual column IDs/row count (never trusted blindly)
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
function returns (or raises ``ModelPlanError``).

PRODUCTION DEFECT CLOSURE (PR #93): a real production request over a
table with a column named ``"Предоплата, Цена с НДС"`` (containing an
internal comma) failed to execute -- Railway logs proved the model call
itself succeeded (HTTP 200), but the turn still fell through to managed-
agent product selection/enrichment. Root cause, reproduced against a
REAL (unmocked) model call in this fix's own test suite: the model could
not reliably reproduce that exact column string verbatim (it echoed back
only ``"Предоплата"``, truncated at the internal comma), so column
validation failed -- and that VALIDATION failure was silently folded into
the exact same ``ModelPlanNotApplicable`` used for a genuine "this is not
a table operation" judgment, which the caller could not distinguish from
a technical failure and therefore let the turn fall through to the
managed agent.

Two closures:

1. The model NEVER has to reproduce a column name at all -- the prompt
   exposes each column under a stable, deterministic id (``c0``, ``c1``,
   ...); the model returns ``column_id``, and this module resolves it
   back to the exact real column name. An unknown id fails validation --
   no fuzzy matching after the fact.
2. ``ModelPlanError`` now carries a ``status`` (``NOT_APPLICABLE`` /
   ``MODEL_ERROR`` / ``PARSE_ERROR`` / ``VALIDATION_ERROR``) and a
   ``reason_code``, so a caller can -- and, per
   ``business_assistant.conversation_gateway``, MUST -- treat a genuine,
   validly-parsed ``NOT_APPLICABLE`` judgment differently from every
   other (technical) failure: only ``NOT_APPLICABLE`` may fall through to
   non-table routing; every other status must fail closed instead of
   being silently reinterpreted as an unrelated workflow.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from data_intel.contracts import (
    ROLE_PRICE,
    ROLE_PURCHASE_PRICE,
    ROLE_SELLING_PRICE,
    TableDescriptor,
)
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

_LOGGER = logging.getLogger("data_intel.nl_plan_llm")

# Bounded, typed outcome of ONE model-plan attempt (observability
# requirement, PR #93): every attempt ends in exactly one of these,
# never a bare boolean/opaque exception string.
STATUS_OK = "OK"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
STATUS_MODEL_ERROR = "MODEL_ERROR"
STATUS_PARSE_ERROR = "PARSE_ERROR"
STATUS_VALIDATION_ERROR = "VALIDATION_ERROR"

# THE fail-open/fail-closed line this module's caller must draw: only a
# VALID, successfully-parsed model judgment that this text is not a
# table operation may defer to other routing. Every other status is a
# TECHNICAL failure of this boundary itself, not a judgment about the
# user's text, and must never be silently reinterpreted as some other
# workflow (see module docstring / PR #93).
NON_TECHNICAL_STATUSES = (STATUS_OK, STATUS_NOT_APPLICABLE)

REASON_MODEL_CALL_FAILED = "model_call_failed"
REASON_MODEL_NOT_CONFIGURED = "model_not_configured"
REASON_NOT_JSON = "model_output_not_json"
REASON_INVALID_KIND = "invalid_or_missing_kind"
REASON_NO_OPERATIONS = "no_operations"
REASON_MALFORMED_OPERATION = "malformed_operation"
REASON_DISALLOWED_OPERATION = "disallowed_operation"
REASON_MISSING_COLUMN_ID = "missing_column_id"
REASON_UNKNOWN_COLUMN_ID = "unknown_column_id"
REASON_INVALID_NUMERIC_VALUE = "invalid_numeric_value"
REASON_INVALID_OPERATOR = "invalid_operator"
REASON_MISSING_NEW_COLUMN = "missing_new_column"
REASON_MISSING_NEW_NAME = "missing_new_name"
REASON_INVALID_LIMIT = "invalid_limit"
REASON_MALFORMED_SCOPE = "malformed_scope"
REASON_UNKNOWN_SCOPE_KIND = "unknown_scope_kind"
REASON_INVALID_ROW_RANGE = "invalid_row_range"
REASON_ROW_RANGE_OUT_OF_BOUNDS = "row_range_out_of_bounds"
REASON_MODEL_MARKED_NOT_APPLICABLE = "model_marked_not_applicable"
# Retail-vs-purchase price separation (production defect closure): a
# ``percent_round`` (in-place mutation) targeting a column whose OWN
# semantic role is the OPPOSITE of the operation's declared ``price_role``
# (e.g. the user asked about RETAIL price but the only column that exists
# is classified as PURCHASE/supplier price) would silently corrupt that
# other price concept -- see ``_validate_operation``'s ``price_role``
# guard below. Fails closed instead (a clear, explicit unresolved state,
# never a silent overwrite).
REASON_PRICE_ROLE_MISMATCH = "price_role_mismatch"
# No canonical selection exists for a request that requires one (a
# conversational field question / write-plan preview about "this
# product" with nothing currently selected).
REASON_NO_SELECTION = "no_current_selection"

# kind discriminator the model itself must return (PR #93: replaces the
# previous single "applicable" boolean with an explicit, harder-to-
# misinterpret two-value tag). PR #94 added ``product_selection``.
# Production defect closure (selected-product conversational continuity):
# ``field_query`` (a specific-attribute question about the currently
# selected row) and ``write_plan_query`` (a read-only "what would be
# written to Bitrix for the current product" question) let this SAME one-
# shot model call also resolve those two conversational shapes
# deterministically instead of falling back to brittle RU/EN regex/stem
# matching (``business_assistant.action_continuation.is_bitrix_write_plan_
# question`` et al) that only recognizes a fixed set of hardcoded phrases.
KIND_TABLE_OPERATION = "table_operation"
KIND_NOT_APPLICABLE = "not_applicable"
KIND_PRODUCT_SELECTION = "product_selection"
KIND_FIELD_QUERY = "field_query"
KIND_WRITE_PLAN_QUERY = "write_plan_query"


class ModelProductSelection(Exception):
    """Validated semantic request to select/inspect one canonical row."""

    def __init__(self, selector_kind: str, value: Any):
        self.selector_kind = selector_kind
        self.value = value
        super().__init__(f"{selector_kind}:{value}")


class ModelFieldQuery(Exception):
    """Validated semantic request for the value of ONE specific attribute
    of the currently selected row (e.g. "what quantity does this product
    have"). ``column_name`` is the resolved real column, or ``None`` when
    the model judged that no such attribute exists in this table at all
    (a genuine "the source data doesn't have this" case, never guessed).
    ``field_label`` is a short, free-text echo of what the user asked
    about -- used ONLY for a user-facing "no such field" message, never
    for logic/lookup."""

    def __init__(self, column_name: str | None, field_label: str = ""):
        self.column_name = column_name
        self.field_label = field_label
        super().__init__(f"field_query:{column_name}")


class ModelWritePlanQuery(Exception):
    """Validated semantic request to preview what would be written to
    Bitrix/Aspro for the CURRENTLY SELECTED product -- read-only, resolved
    entirely from canonical Workset selection state by the caller (never
    a second interpretation of ``text``)."""

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
# Operations that reference an EXISTING column -- resolved via
# ``column_id`` (never a raw name the model would have to reproduce).
_COLUMN_ID_REQUIRED_OPS = (
    OP_PERCENT_ROUND,
    OP_ADD_COLUMN_PERCENT,
    OP_FILTER_CONTAINS,
    OP_FILTER_COMPARE,
    OP_SORT,
    OP_REMOVE_COLUMN,
    OP_RENAME_COLUMN,
)
SCOPE_SELECTED = "selected"
_SCOPE_KINDS = (SCOPE_ALL, SCOPE_ROW_RANGE, SCOPE_REMAINDER, SCOPE_SELECTED)


class ModelPlanError(Exception):
    """Raised for EVERY outcome other than "the model produced a strictly
    valid, executable table-wide plan". Carries a typed ``status``
    (``STATUS_NOT_APPLICABLE`` for a genuine, validly-parsed "this is not
    a table operation" model judgment; ``STATUS_MODEL_ERROR``/
    ``STATUS_PARSE_ERROR``/``STATUS_VALIDATION_ERROR`` for a TECHNICAL
    failure of this boundary itself) and a ``reason_code`` -- see the
    ``REASON_*`` constants above. The caller MUST distinguish these:
    only ``STATUS_NOT_APPLICABLE`` may defer to non-table routing; every
    other status is a technical failure that must fail closed instead of
    being silently reinterpreted as an unrelated workflow (PR #93)."""

    def __init__(self, status: str, reason_code: str, detail: str = ""):
        self.status = status
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{status}:{reason_code}" + (f" ({detail})" if detail else ""))


# Backward-compatible alias: PR #92 raised ``ModelPlanNotApplicable`` for
# every non-"OK" outcome. Existing callers that only need "should I defer
# to legacy routing" (never inspecting status) keep working unchanged.
ModelPlanNotApplicable = ModelPlanError


def _log_attempt(
    *, status: str, reason_code: str, operation_count: int = 0, scope_kinds: tuple[str, ...] = ()
) -> None:
    """Bounded observability (PR #93 requirement 1): ONE concise log line
    per model-plan attempt -- status/reason_code/operation_count/scope
    kinds only. Never logs API keys, prompts, workbook contents, or
    dataset rows."""

    _LOGGER.info(
        "canonical_table_model_plan status=%s reason_code=%s operation_count=%d scope_kinds=%s",
        status,
        reason_code or "-",
        operation_count,
        ",".join(scope_kinds) if scope_kinds else "-",
    )


def _fail(status: str, reason_code: str, detail: str = "") -> None:
    _log_attempt(status=status, reason_code=reason_code)
    raise ModelPlanError(status, reason_code, detail)


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


def _column_ids(table: TableDescriptor) -> dict[str, str]:
    """Stable, deterministic ``c<index>`` id -> real column name mapping
    (PR #93 requirement 3): the model is NEVER asked to reproduce an
    arbitrary source column string (e.g. one containing punctuation like
    ``"Предоплата, Цена с НДС"``) verbatim -- it only ever has to copy
    back a short id it was just given. Index-based, so it is stable for
    the lifetime of one prompt/response round trip regardless of how
    unusual any actual column name is."""

    return {f"c{i}": c.source_name for i, c in enumerate(table.columns)}


# Retail-vs-purchase price separation (production defect closure): the
# ONLY roles annotated in the prompt -- a bare tag next to a column id
# ("[purchase price]"/"[retail/selling price]"/"[undifferentiated price]"),
# never the raw internal role string -- so the model can tell an EXISTING
# retail/selling-price column from an EXISTING purchase/supplier-price
# column (and from a table that has neither split out) without ever
# having to guess from the column's own name.
_PRICE_ROLE_TAGS = {
    ROLE_PURCHASE_PRICE: "purchase price",
    ROLE_SELLING_PRICE: "retail/selling price",
    ROLE_PRICE: "undifferentiated price (no separate purchase/retail split)",
}


def _column_role_tags(table: TableDescriptor) -> dict[str, str]:
    return {
        f"c{i}": _PRICE_ROLE_TAGS[c.semantic_role]
        for i, c in enumerate(table.columns)
        if c.semantic_role in _PRICE_ROLE_TAGS
    }


def _column_roles(table: TableDescriptor) -> dict[str, str]:
    return {f"c{i}": c.semantic_role for i, c in enumerate(table.columns)}


def _build_prompt(
    text: str, table: TableDescriptor, row_count: int, *, selected_row_index: int | None = None
) -> str:
    column_ids = _column_ids(table)
    role_tags = _column_role_tags(table)
    column_lines = "\n".join(
        f'  {cid} = "{name}"' + (f" [{role_tags[cid]}]" if cid in role_tags else "")
        for cid, name in column_ids.items()
    )
    return (
        "You translate ONE user request about a data table into STRICT JSON. "
        "Respond with ONLY a single JSON object -- no prose, no markdown code fences, "
        "no explanation before or after it.\n\n"
        f"Table columns (id = exact name):\n{column_lines}\n"
        f"Current row count: {row_count}\n"
        f"Conversation selection: {'row ' + str(selected_row_index) if selected_row_index is not None else 'none'}\n\n"
        "Output JSON schema:\n"
        "{\n"
        '  "kind": "table_operation" | "product_selection" | "field_query" | "write_plan_query" | "not_applicable",\n'
        '  "selector": {"kind": "ordinal" | "identifier" | "current", "value": <zero-based integer for ordinal, exact identifier text for identifier>},\n'
        '  "column_id": <one of the column ids above, or null if no such attribute exists in this table -- ONLY for "field_query">,\n'
        '  "field_label": <short free-text echo of the attribute the user asked about, e.g. "quantity" -- ONLY for "field_query", used only to phrase a "no such data" reply, never for lookup>,\n'
        '  "wants_workbook": <bool, only for "table_operation">,\n'
        '  "operations": [\n'
        "    {\n"
        '      "scope": {"kind": "all" | "selected" | "row_range" | "remainder", "start": <int, only for row_range>, "end": <int, only for row_range>},\n'
        '      "column_id": <one of the column ids above, e.g. "c0" -- NEVER the actual column name>,\n'
        '      "operation": "percent_round" | "add_column_percent" | "filter_contains" | "filter_compare" | "sort" | "limit" | "remove_column" | "rename_column" | "dedup",\n'
        '      "value": <string or number, meaning depends on "operation" -- e.g. the signed percent for percent_round/add_column_percent, the comparison operator threshold for filter_compare, the substring for filter_contains, the row count for limit>,\n'
        '      "operator": <one of ">"|"<"|">="|"<=" -- ONLY for filter_compare>,\n'
        '      "new_column": <string, a brand-new column NAME (not an id) -- ONLY for add_column_percent/rename_column>,\n'
        '      "price_role": "purchase" | "retail" | null, <ONLY for percent_round/add_column_percent when the value being set is specifically a PURCHASE/supplier price or specifically a RETAIL/selling price; null/omit when the request does not distinguish (e.g. a single undifferentiated price column)>\n'
        '      "descending": <bool -- ONLY for sort>\n'
        "    }\n"
        '  ] (omit/empty for "not_applicable"/"product_selection"/"field_query"/"write_plan_query")\n'
        "}\n\n"
        'Use "column_id" (e.g. "c0") for EVERY reference to an EXISTING column -- copy the short id exactly '
        "as given, never the column's actual name (some column names contain punctuation/commas and are easy "
        "to reproduce incorrectly; the id avoids that entirely). Only \"new_column\" for add_column_percent/"
        "rename_column is a real, brand-new NAME you invent (it does not exist yet, so it has no id).\n\n"
        'Set "kind" to "product_selection" when the user asks to select, navigate to, inspect, or show the card '
        'of one row. Resolve any natural-language ordinal in any language to a zero-based integer; copy an '
        'explicit SKU/EAN/article as an identifier; use current only for the already selected item. A message '
        "consisting of ONLY a product identifier/SKU/article-looking value, with no other instruction, is also "
        'a "product_selection" (selector kind "identifier").\n'
        'Set "kind" to "field_query" ONLY when a current selection is supplied (see "Conversation selection" '
        'above) AND the user asks about ONE specific attribute/value of that already-selected item (e.g. its '
        "quantity/stock, EAN, category, brand, a specific price) rather than asking to see the whole card/"
        'summary or to change anything. Resolve the attribute to a "column_id" from the schema above if such a '
        'column exists; set "column_id" to null (and fill "field_label") if the table has no such column at '
        "all -- never guess/invent a value. With no current selection, this is never applicable.\n"
        'Set "kind" to "write_plan_query" ONLY when a current selection is supplied AND the user asks, in any '
        "wording or language, to preview/see/explain what data would be uploaded/written/recorded/published "
        "for the CURRENTLY SELECTED product to an external system (e.g. Bitrix/Aspro/CRM) without yet "
        "approving/confirming an actual write. This is a READ-ONLY preview question, resolved entirely from "
        'already-known selected-product state -- it never has "operations" of its own. With no current '
        "selection, this is never applicable.\n"
        'Set "kind" to "not_applicable" for everything else that is NOT a table transform/filter/sort/'
        "deduplicate, NOT a row selection, NOT a specific-attribute question about the current selection, and "
        "NOT a write-plan preview question -- for example: analyzing/summarizing the whole table, asking to "
        'prepare/enrich a full product card, or an actual write/publish confirmation. Set "kind" to '
        '"table_operation" for a genuine table-wide (or table-subset) mutation/filter/sort/dedup request.\n'
        'When a current selection is supplied and the request changes that item by conversational reference '
        '(for example "it", "this product", or an omitted subject), use scope "selected". Never turn that '
        'into scope "all". If the user explicitly requests multiple rows/the whole table, use row_range, '
        'remainder, or all instead: a selection is a default conversational focus, not a restriction. If no '
        'selection is supplied, scope "selected" is invalid. These rules are semantic and apply in every '
        'language. A simultaneous request to show the card does not make the local table change inapplicable.\n'
        "IMPORTANT: a request to modify/filter/sort rows in THIS table is a table_operation even when the "
        "SAME request also explicitly says not to write/publish/export the result anywhere else (e.g. "
        '"...do not write this to Bitrix/CRM/any external system") -- that is a separate, local-only-scope '
        "instruction about where the result must NOT go, not a reason to call the table change itself "
        "not_applicable. This is a general rule about ANY external-system qualifier, not specific wording.\n"
        'To modify an EXISTING column\'s values in place (the common case, e.g. "increase the price in this '
        'column by X%"), use "percent_round" on that column\'s id. Use "add_column_percent" ONLY when the '
        "user explicitly asks to ADD A NEW, additional column (e.g. a separate margin/markup column) rather "
        "than changing an existing one.\n"
        "Purchase price and retail/selling price are always two SEPARATE business values, even when the "
        'table has only one price column. When a percent_round/add_column_percent request concerns '
        'RETAIL/selling price specifically, set "price_role" to "retail"; when it concerns PURCHASE/supplier/'
        'cost price specifically, set "price_role" to "purchase" -- the column tags above ("[purchase '
        'price]"/"[retail/selling price]"/"[undifferentiated price]") tell you which, if any, EXISTING column '
        "already represents each concept. If the request concerns RETAIL price but the ONLY existing price "
        "column is tagged purchase price (there is no separate retail/selling column yet), you MUST use "
        '"add_column_percent" to create a NEW, separate column for it (never "percent_round" on that purchase-'
        "price column -- that would silently destroy the original purchase price). Symmetrically, never use "
        '"percent_round" on a retail/selling-price column for a request that concerns purchase price. Leave '
        '"price_role" null/omitted whenever the request does not distinguish purchase from retail (e.g. the '
        'table already has a single undifferentiated "price" column and the user just says "price").\n'
        'Use "row_range" with a 0-based, half-open [start, end) row-index window for an explicit ordinal '
        'subset (e.g. "the first 3 rows" is start=0, end=3). Prefer "remainder" (rather than an explicit '
        'row_range covering the tail) for "everyone else"/"the rest"/"all other rows" -- every row NOT '
        "covered by an earlier rule in THIS SAME response -- so the plan stays correct even if the actual "
        'row count differs from what you assumed. You may emit several rules, each with its own scope, to '
        'express a compound request such as "the first N rows get X, everyone else gets Y". Use "all" when '
        "a rule applies to the whole table.\n"
        "Never invent row values, and never describe or produce any code -- only the operations listed "
        "above, applied by an existing deterministic engine.\n\n"
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
        _fail(STATUS_VALIDATION_ERROR, REASON_INVALID_NUMERIC_VALUE, repr(value))


def _validate_scope(
    raw_scope: Any, *, row_count: int, selected_row_index: int | None = None
) -> OperationScope:
    if raw_scope is None:
        return OperationScope(kind=SCOPE_ALL)
    if not isinstance(raw_scope, dict):
        _fail(STATUS_PARSE_ERROR, REASON_MALFORMED_SCOPE)
    kind = str(raw_scope.get("kind") or SCOPE_ALL)
    if kind not in _SCOPE_KINDS:
        _fail(STATUS_VALIDATION_ERROR, REASON_UNKNOWN_SCOPE_KIND, kind)
    if kind in (SCOPE_ALL, SCOPE_REMAINDER):
        return OperationScope(kind=kind)
    if kind == SCOPE_SELECTED:
        if selected_row_index is None or not 0 <= selected_row_index < row_count:
            _fail(STATUS_VALIDATION_ERROR, REASON_INVALID_ROW_RANGE, "selected")
        return OperationScope(kind=SCOPE_ROW_RANGE, start=selected_row_index, end=selected_row_index + 1)
    try:
        start = int(raw_scope.get("start"))
        end = int(raw_scope.get("end"))
    except (TypeError, ValueError):
        _fail(STATUS_PARSE_ERROR, REASON_INVALID_ROW_RANGE)
    if start < 0 or end < start or end > row_count:
        _fail(STATUS_VALIDATION_ERROR, REASON_ROW_RANGE_OUT_OF_BOUNDS, f"{start}-{end}/{row_count}")
    return OperationScope(kind=SCOPE_ROW_RANGE, start=start, end=end)


def _resolve_column_id(raw_op: dict, *, column_by_id: dict[str, str]) -> str:
    """Deterministic ``column_id`` -> real column name resolution (PR #93
    requirement 3): an unknown id fails validation outright -- no fuzzy/
    best-effort name guessing after the model response."""

    column_id = raw_op.get("column_id")
    if column_id is None:
        _fail(STATUS_VALIDATION_ERROR, REASON_MISSING_COLUMN_ID)
    column_id = str(column_id).strip()
    resolved = column_by_id.get(column_id)
    if resolved is None:
        _fail(STATUS_VALIDATION_ERROR, REASON_UNKNOWN_COLUMN_ID, column_id)
    return resolved


def _validate_operation(
    raw_op: Any,
    *,
    column_by_id: dict[str, str],
    column_role_by_id: dict[str, str],
    row_count: int,
    selected_row_index: int | None = None,
) -> PlannedOperation:
    if not isinstance(raw_op, dict):
        _fail(STATUS_PARSE_ERROR, REASON_MALFORMED_OPERATION)
    op_name = str(raw_op.get("operation") or "")
    if op_name not in _ALLOWED_OPS:
        _fail(STATUS_VALIDATION_ERROR, REASON_DISALLOWED_OPERATION, op_name)
    scope = _validate_scope(
        raw_op.get("scope"), row_count=row_count, selected_row_index=selected_row_index
    )

    column = None
    column_role = None
    if op_name in _COLUMN_ID_REQUIRED_OPS:
        column = _resolve_column_id(raw_op, column_by_id=column_by_id)
        column_role = column_role_by_id.get(str(raw_op.get("column_id") or "").strip())

    # Retail-vs-purchase price separation (production defect closure): the
    # model's OWN declared semantic intent for THIS operation (never
    # guessed here) is validated against the TARGET column's own already-
    # classified role -- see the ``price_role`` prompt instructions in
    # ``_build_prompt``. An unrecognized/absent value is simply advisory-
    # off (``None``), preserving every pre-existing plan that never sent
    # this new, optional field at all.
    price_role = None
    if op_name in (OP_PERCENT_ROUND, OP_ADD_COLUMN_PERCENT):
        raw_price_role = raw_op.get("price_role")
        if raw_price_role in ("purchase", "retail"):
            price_role = raw_price_role

    value = raw_op.get("value")
    if op_name == OP_PERCENT_ROUND:
        if price_role == "retail" and column_role == ROLE_PURCHASE_PRICE:
            # The user's request concerns RETAIL price, but the ONLY
            # column the model could target is the EXISTING purchase/
            # supplier price -- an in-place ``percent_round`` here would
            # silently overwrite (and then mislabel) that purchase price.
            # Fail closed instead: never a silent overwrite (see module
            # docstring / production defect closure).
            _fail(STATUS_VALIDATION_ERROR, REASON_PRICE_ROLE_MISMATCH, "retail_on_purchase_price_column")
        if price_role == "purchase" and column_role == ROLE_SELLING_PRICE:
            _fail(STATUS_VALIDATION_ERROR, REASON_PRICE_ROLE_MISMATCH, "purchase_on_retail_price_column")
        pct = _validate_decimal(value)
        params = {"column": column, "percent": str(pct), "round_mode": None, "round_to": None}
    elif op_name == OP_ADD_COLUMN_PERCENT:
        pct = _validate_decimal(value)
        new_column = raw_op.get("new_column")
        if not isinstance(new_column, str) or not new_column.strip():
            _fail(STATUS_VALIDATION_ERROR, REASON_MISSING_NEW_COLUMN)
        params = {
            "source_column": column,
            "new_column": new_column.strip(),
            "percent": str(pct),
            "price_role": price_role,
        }
    elif op_name == OP_FILTER_CONTAINS:
        params = {"column": column, "value": str(value if value is not None else "")}
    elif op_name == OP_FILTER_COMPARE:
        operator = str(raw_op.get("operator") or "")
        if operator not in (">", "<", ">=", "<="):
            _fail(STATUS_VALIDATION_ERROR, REASON_INVALID_OPERATOR, operator)
        threshold = _validate_decimal(value)
        params = {"column": column, "operator": operator, "value": str(threshold)}
    elif op_name == OP_SORT:
        params = {"column": column, "descending": bool(raw_op.get("descending"))}
    elif op_name == OP_LIMIT:
        try:
            n = int(value)
        except (TypeError, ValueError):
            _fail(STATUS_VALIDATION_ERROR, REASON_INVALID_LIMIT, repr(value))
        if n < 0:
            _fail(STATUS_VALIDATION_ERROR, REASON_INVALID_LIMIT, repr(value))
        params = {"n": n}
    elif op_name == OP_REMOVE_COLUMN:
        params = {"column": column}
    elif op_name == OP_RENAME_COLUMN:
        new_name = raw_op.get("new_column") or value
        if not isinstance(new_name, str) or not new_name.strip():
            _fail(STATUS_VALIDATION_ERROR, REASON_MISSING_NEW_NAME)
        params = {"column": column, "new_name": new_name.strip()}
    elif op_name == OP_DEDUP:
        params = {"remove": bool(value)}
    else:  # pragma: no cover -- unreachable, _ALLOWED_OPS already checked above
        _fail(STATUS_VALIDATION_ERROR, REASON_DISALLOWED_OPERATION, op_name)

    return PlannedOperation(op_name, params, scope=scope)


async def compile_request_via_model(
    text: str,
    table: TableDescriptor,
    row_count: int,
    *,
    model_call: ModelCall | None = None,
    selected_row_index: int | None = None,
) -> OperationPlan:
    """Make ONE model call to interpret ``text`` against ``table``'s own
    schema (exposed as stable column ids, never raw names -- see
    ``_column_ids``), then DETERMINISTICALLY parse and validate its JSON
    response into a validated ``OperationPlan`` -- never trusting the
    model's output as-is. Raises ``ModelPlanError`` for every non-
    genuine-table-operation outcome; ``exc.status`` distinguishes a valid
    model judgment (``STATUS_NOT_APPLICABLE`` -- safe to defer to other
    routing) from a technical failure of this boundary itself
    (``STATUS_MODEL_ERROR``/``STATUS_PARSE_ERROR``/
    ``STATUS_VALIDATION_ERROR`` -- the caller must fail closed instead of
    silently reinterpreting the request as an unrelated workflow; see
    module docstring / PR #93)."""

    column_by_id = _column_ids(table)
    column_role_by_id = _column_roles(table)
    prompt = _build_prompt(text, table, row_count, selected_row_index=selected_row_index)

    if model_call is None:
        try:
            caller = _default_model_call()
        except Exception as exc:
            # No default model call could even be constructed (e.g.
            # OPENAI_API_KEY/OPENAI_MODEL are not configured in this
            # deployment/environment at all -- see
            # ``agents.openai_agent.OpenAIAgent.__init__``). This is NOT a
            # technical failure of an ATTEMPTED call: canonical-table-
            # execution via the model is simply UNAVAILABLE here, exactly
            # as if this whole boundary did not exist. Defer to legacy/
            # managed-agent routing (``STATUS_NOT_APPLICABLE``) instead of
            # failing closed -- failing closed here would turn "the model
            # feature isn't configured in this deployment" into "every
            # Excel-family turn now returns a 'not executed' error",
            # which is a regression, not a fix.
            _fail(STATUS_NOT_APPLICABLE, REASON_MODEL_NOT_CONFIGURED, str(exc))
    else:
        caller = model_call

    try:
        raw_text = await caller(prompt)
    except ModelPlanError:
        raise
    except Exception as exc:
        # The provider WAS configured/reachable enough to attempt a call,
        # and that attempt itself failed (network error, timeout, bad
        # HTTP status, ...) -- a genuine TECHNICAL failure of this
        # boundary, per PR #93 this must fail closed, never be silently
        # reinterpreted as "not a table operation".
        _fail(STATUS_MODEL_ERROR, REASON_MODEL_CALL_FAILED, str(exc))

    payload = _parse_json_object(raw_text)
    if not isinstance(payload, dict):
        _fail(STATUS_PARSE_ERROR, REASON_NOT_JSON)

    kind = payload.get("kind")
    if kind == KIND_PRODUCT_SELECTION:
        selector = payload.get("selector")
        if not isinstance(selector, dict):
            _fail(STATUS_PARSE_ERROR, REASON_INVALID_KIND, "missing selector")
        selector_kind = str(selector.get("kind") or "")
        value = selector.get("value")
        if selector_kind == "ordinal":
            try:
                value = int(value)
            except (TypeError, ValueError):
                _fail(STATUS_VALIDATION_ERROR, REASON_INVALID_ROW_RANGE, repr(value))
            if value < 0 or value >= row_count:
                _fail(STATUS_VALIDATION_ERROR, REASON_ROW_RANGE_OUT_OF_BOUNDS, str(value))
        elif selector_kind == "identifier":
            value = str(value or "").strip()
            if not value:
                _fail(STATUS_VALIDATION_ERROR, REASON_INVALID_KIND, "empty identifier")
        elif selector_kind == "current":
            if selected_row_index is None:
                _fail(STATUS_VALIDATION_ERROR, REASON_INVALID_ROW_RANGE, "no current selection")
            value = selected_row_index
        else:
            _fail(STATUS_VALIDATION_ERROR, REASON_INVALID_KIND, selector_kind)
        raise ModelProductSelection(selector_kind, value)
    if kind == KIND_FIELD_QUERY:
        # Production defect closure (selected-product conversational
        # continuity): a specific-attribute question about the CURRENTLY
        # selected row -- requires a real canonical selection to answer
        # from; with none, this kind is simply invalid for this turn
        # (the model was told exactly that in the prompt).
        if selected_row_index is None or not 0 <= selected_row_index < row_count:
            _fail(STATUS_VALIDATION_ERROR, REASON_NO_SELECTION)
        field_label = str(payload.get("field_label") or "").strip()
        if "column_id" not in payload:
            _fail(STATUS_PARSE_ERROR, REASON_MISSING_COLUMN_ID)
        raw_column_id = payload.get("column_id")
        if raw_column_id is None:
            # A genuine, validly-parsed judgment that this table has no
            # such attribute at all -- never a technical failure.
            raise ModelFieldQuery(None, field_label)
        column_id = str(raw_column_id).strip()
        resolved = column_by_id.get(column_id)
        if resolved is None:
            _fail(STATUS_VALIDATION_ERROR, REASON_UNKNOWN_COLUMN_ID, column_id)
        raise ModelFieldQuery(resolved, field_label)
    if kind == KIND_WRITE_PLAN_QUERY:
        if selected_row_index is None or not 0 <= selected_row_index < row_count:
            _fail(STATUS_VALIDATION_ERROR, REASON_NO_SELECTION)
        raise ModelWritePlanQuery()
    if kind == KIND_NOT_APPLICABLE:
        _fail(STATUS_NOT_APPLICABLE, REASON_MODEL_MARKED_NOT_APPLICABLE)
    if kind != KIND_TABLE_OPERATION:
        _fail(STATUS_PARSE_ERROR, REASON_INVALID_KIND, repr(kind))

    raw_ops = payload.get("operations")
    if not isinstance(raw_ops, list) or not raw_ops:
        _fail(STATUS_PARSE_ERROR, REASON_NO_OPERATIONS)

    operations = tuple(
        _validate_operation(
            raw_op,
            column_by_id=column_by_id,
            column_role_by_id=column_role_by_id,
            row_count=row_count,
            selected_row_index=selected_row_index,
        )
        for raw_op in raw_ops
    )

    plan = OperationPlan(
        operations=operations,
        wants_workbook=bool(payload.get("wants_workbook")),
        raw_text=text,
        intent_summary=", ".join(op.op for op in operations),
    )
    _log_attempt(
        status=STATUS_OK,
        reason_code="",
        operation_count=len(operations),
        scope_kinds=tuple(op.scope.kind for op in operations),
    )
    return plan
