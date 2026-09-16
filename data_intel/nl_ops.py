"""Deterministic natural-language -> data-operation compiler (Block 5.1).

LLM boundary (spec sections 5, 6, 19): this module NEVER calls a model and
NEVER estimates a number. It recognizes a bounded set of common RU/EN
phrasings for filter / sort / percent-and-round / column / dedup / export
requests and compiles them into a validated, typed ``OperationPlan``. The
plan is executed by ``data_intel.transform`` -- a separate, purely
deterministic engine. Ambiguous column/operator references (for example two
distinct price columns when the request just says "цена") raise
``AmbiguousOperationError`` instead of guessing, so the caller can route the
turn to the existing conversational clarification flow
(``business_assistant/action_continuation.py``).

Nothing here is a general-purpose expression evaluator: only the specific,
whitelisted operation shapes below are ever produced. There is no arbitrary
model-generated code execution path (spec section 16/19).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from data_intel.cleaning import normalize_decimal_string
from data_intel.contracts import (
    ROLE_BRAND,
    ROLE_CATEGORY,
    ROLE_PRICE,
    ROLE_PRODUCT_NAME,
    ROLE_PURCHASE_PRICE,
    ROLE_SELLING_PRICE,
    ROLE_STOCK,
    ColumnDescriptor,
    TableDescriptor,
)
from data_intel.errors import DataIntelError

OP_FILTER_CONTAINS = "filter_contains"
OP_FILTER_COMPARE = "filter_compare"
OP_SORT = "sort"
OP_LIMIT = "limit"
OP_PERCENT_ROUND = "percent_round"
OP_ADD_COLUMN_PERCENT = "add_column_percent"
OP_REMOVE_COLUMN = "remove_column"
OP_RENAME_COLUMN = "rename_column"
OP_DEDUP = "dedup"
OP_TEXT_NORMALIZE = "text_normalize"

DATA_OPERATION_AMBIGUOUS = "data_operation_ambiguous"
DATA_OPERATION_UNSUPPORTED = "data_operation_unsupported"

_PRICE_ROLES = (ROLE_SELLING_PRICE, ROLE_PRICE, ROLE_PURCHASE_PRICE)
_TEXT_MATCH_ROLES = (ROLE_BRAND, ROLE_PRODUCT_NAME, ROLE_CATEGORY)

_ROUND_UP_STEMS = ("округли вверх", "округлить вверх", "round up", "roundup")
_ROUND_DOWN_STEMS = ("округли вниз", "округлить вниз", "round down", "rounddown")
_ROUND_NEAREST_STEMS = ("округли до", "round to nearest", "round to the nearest")

_SAVE_STEMS = (
    "сохрани",
    "сохранить",
    "сделай новый excel",
    "сделай excel",
    "новый excel",
    "экспортируй",
    "экспорт в excel",
    "save as excel",
    "save to excel",
    "export to excel",
    "export as excel",
    "скачать excel",
    "download excel",
)
_DEDUP_STEMS = ("дубликат", "duplicate")
_KEEP_ONLY_RE = re.compile(
    r"(?:оставь(?:те)?\s+только|оставить\s+только|только|keep only)\s+"
    # Negative lookahead: "только дешевле 50000" / "только дороже 30000" is a
    # bare price-comparison qualifier, not a text-value filter -- must not be
    # captured here (that is _compile_price_filters' job below).
    r"(?!дороже\b|дешевле\b|от\s+\d|between\b|более\b|менее\b)(.+?)"
    r"(?=$|дороже|дешевле|от\s+\d|between|более|менее|,|\.|;)",
    re.I,
)
_TOP_EXPENSIVE_STEMS = ("самые дорог", "most expensive", "дороже всего")
_TOP_CHEAP_STEMS = ("самые дешев", "самые дешёв", "cheapest", "дешевле всего")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold().replace("ё", "е"))


def _has_stem(text: str, stems: tuple[str, ...]) -> bool:
    blob = _norm(text)
    return any(_norm(stem) in blob for stem in stems if stem)


class AmbiguousOperationError(DataIntelError):
    """Raised when the NL request cannot be safely, uniquely compiled."""

    def __init__(self, message_safe: str, *, candidates: list[str] | None = None):
        super().__init__(DATA_OPERATION_AMBIGUOUS)
        self.message_safe = message_safe
        self.candidates = list(candidates or [])


class UnsupportedOperationError(DataIntelError):
    def __init__(self, message_safe: str = "Не удалось распознать операцию над таблицей."):
        super().__init__(DATA_OPERATION_UNSUPPORTED)
        self.message_safe = message_safe


@dataclass(frozen=True)
class PlannedOperation:
    op: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", dict(self.params or {}))


@dataclass(frozen=True)
class OperationPlan:
    operations: tuple[PlannedOperation, ...]
    wants_workbook: bool = False
    raw_text: str = ""
    intent_summary: str = ""


def _find_amounts(text: str) -> list[Decimal]:
    """Extract numbers, tolerating thousands separated by spaces (30 000)."""

    blob = text.replace("\u00a0", " ")
    out: list[Decimal] = []
    for match in re.finditer(r"\d[\d\s]*(?:[.,]\d+)?", blob):
        candidate = match.group(0).strip()
        if not candidate or candidate.isspace():
            continue
        norm = normalize_decimal_string(candidate.replace(" ", ""))
        if norm is None:
            continue
        try:
            out.append(Decimal(norm))
        except InvalidOperation:
            continue
    return out


def _price_columns(table: TableDescriptor) -> list[ColumnDescriptor]:
    return [c for c in table.columns if c.semantic_role in _PRICE_ROLES]


def _resolve_price_column(table: TableDescriptor, text: str) -> ColumnDescriptor:
    candidates = _price_columns(table)
    if not candidates:
        raise UnsupportedOperationError("В таблице не найден столбец с ценой.")
    if len(candidates) == 1:
        return candidates[0]
    blob = _norm(text)
    if _has_stem(blob, ("закупочн", "purchase", "cost")):
        for c in candidates:
            if c.semantic_role == ROLE_PURCHASE_PRICE:
                return c
    if _has_stem(blob, ("продажн", "розничн", "selling", "retail")):
        for c in candidates:
            if c.semantic_role == ROLE_SELLING_PRICE:
                return c
    names = [c.source_name for c in candidates]
    raise AmbiguousOperationError(
        "В таблице несколько столбцов с ценой ("
        + ", ".join(names)
        + "). Уточните, какой использовать.",
        candidates=names,
    )
def _resolve_text_column(table: TableDescriptor, text: str) -> ColumnDescriptor | None:
    candidates = [c for c in table.columns if c.semantic_role in _TEXT_MATCH_ROLES]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Prefer brand for single-token filter values; product_name is the
    # deterministic fallback for everything else.
    for role in (ROLE_BRAND, ROLE_PRODUCT_NAME, ROLE_CATEGORY):
        for c in candidates:
            if c.semantic_role == role:
                return c
    return candidates[0]


def _compile_keep_only(table: TableDescriptor, text: str, ops: list[PlannedOperation]) -> None:
    match = _KEEP_ONLY_RE.search(text)
    if not match:
        return
    value = match.group(1).strip(" .,;")
    if not value:
        return
    column = _resolve_text_column(table, text)
    if column is None:
        raise UnsupportedOperationError(
            "Не найден текстовый столбец (бренд/название), по которому можно отфильтровать."
        )
    ops.append(
        PlannedOperation(
            OP_FILTER_CONTAINS,
            {"column": column.source_name, "value": value},
        )
    )


def _compile_price_filters(table: TableDescriptor, text: str, ops: list[PlannedOperation]) -> None:
    blob = _norm(text)
    amounts = _find_amounts(text)
    if not amounts:
        return
    price_col = None

    def _col() -> ColumnDescriptor:
        nonlocal price_col
        if price_col is None:
            price_col = _resolve_price_column(table, text)
        return price_col

    range_match = re.search(r"от\s+([\d\s.,]+)\s+до\s+([\d\s.,]+)", blob) or re.search(
        r"between\s+([\d\s.,]+)\s+and\s+([\d\s.,]+)", blob
    )
    if range_match:
        vals = _find_amounts(range_match.group(0))
        if len(vals) >= 2:
            col = _col()
            ops.append(
                PlannedOperation(
                    OP_FILTER_COMPARE,
                    {"column": col.source_name, "operator": ">=", "value": str(vals[0])},
                )
            )
            ops.append(
                PlannedOperation(
                    OP_FILTER_COMPARE,
                    {"column": col.source_name, "operator": "<=", "value": str(vals[1])},
                )
            )
            return
    if _has_stem(blob, ("не дороже", "no more than", "at most")):
        col = _col()
        ops.append(
            PlannedOperation(
                OP_FILTER_COMPARE, {"column": col.source_name, "operator": "<=", "value": str(amounts[0])}
            )
        )
        return
    if _has_stem(blob, ("не дешевле", "no less than", "at least")):
        col = _col()
        ops.append(
            PlannedOperation(
                OP_FILTER_COMPARE, {"column": col.source_name, "operator": ">=", "value": str(amounts[0])}
            )
        )
        return
    if _has_stem(blob, ("дороже", "more expensive than", "above", "over")):
        col = _col()
        ops.append(
            PlannedOperation(
                OP_FILTER_COMPARE, {"column": col.source_name, "operator": ">", "value": str(amounts[0])}
            )
        )
    if _has_stem(blob, ("дешевле", "cheaper than", "below", "under")):
        col = _col()
        ops.append(
            PlannedOperation(
                OP_FILTER_COMPARE, {"column": col.source_name, "operator": "<", "value": str(amounts[0])}
            )
        )


_PERCENT_RE = re.compile(
    r"(минус|плюс|increase|decrease|\+|-)?\s*(\d+(?:[.,]\d+)?)\s*(?:%|процент\w*|percent\w*)", re.I
)


def _compile_percent_round(table: TableDescriptor, text: str, ops: list[PlannedOperation]) -> None:
    blob = _norm(text)
    match = _PERCENT_RE.search(blob)
    if not match:
        return
    sign_word = (match.group(1) or "").strip().lower()
    pct_value = Decimal(match.group(2).replace(",", "."))
    negative = sign_word in {"минус", "decrease", "-"} or _has_stem(
        blob[: match.start()], ("минус", "снизь", "снизить", "уменьши", "decrease")
    )
    if sign_word in {"плюс", "increase", "+"}:
        negative = False
    signed_pct = -pct_value if negative else pct_value

    round_mode = None
    round_to = None
    if _has_stem(blob, _ROUND_UP_STEMS):
        round_mode = "up"
    elif _has_stem(blob, _ROUND_DOWN_STEMS):
        round_mode = "down"
    elif _has_stem(blob, _ROUND_NEAREST_STEMS):
        round_mode = "nearest"
    if round_mode:
        nearest_match = re.search(
            r"(?:ближайш\w*|nearest)\s+(\d+(?:[.,]\d+)?)", blob
        )
        if nearest_match:
            round_to = Decimal(nearest_match.group(1).replace(",", "."))
        else:
            trailing = re.search(r"до\s+(\d+(?:[.,]\d+)?)\s*$", blob) or re.search(
                r"to\s+(\d+(?:[.,]\d+)?)\s*$", blob
            )
            if trailing:
                round_to = Decimal(trailing.group(1).replace(",", "."))
    col = _resolve_price_column(table, text)
    ops.append(
        PlannedOperation(
            OP_PERCENT_ROUND,
            {
                "column": col.source_name,
                "percent": str(signed_pct),
                "round_mode": round_mode,
                "round_to": str(round_to) if round_to is not None else None,
            },
        )
    )


_SORT_DESC_STEMS = ("по убыванию", "descending", "desc")
_SORT_ASC_STEMS = ("по возрастанию", "ascending", "asc")


def _compile_top_n(table: TableDescriptor, text: str, ops: list[PlannedOperation]) -> None:
    blob = _norm(text)
    limit_match = re.search(r"\btop\s*(\d{1,4})\b|\bлучш\w*\s*(\d{1,4})\b", blob)
    limit = None
    if limit_match:
        limit = int(limit_match.group(1) or limit_match.group(2))
    if _has_stem(blob, _TOP_EXPENSIVE_STEMS):
        col = _resolve_price_column(table, text)
        ops.append(PlannedOperation(OP_SORT, {"column": col.source_name, "descending": True}))
        ops.append(PlannedOperation(OP_LIMIT, {"n": limit or 10}))
        return
    if _has_stem(blob, _TOP_CHEAP_STEMS):
        col = _resolve_price_column(table, text)
        ops.append(PlannedOperation(OP_SORT, {"column": col.source_name, "descending": False}))
        ops.append(PlannedOperation(OP_LIMIT, {"n": limit or 10}))
        return
    sort_match = re.search(
        r"(?:отсортируй|сортируй|sort)\w*\s+по\s+([\w\s]+?)(?:\s+(по возрастанию|по убыванию)|$)", blob
    )
    if sort_match:
        col_hint = sort_match.group(1).strip()
        direction = sort_match.group(2) or ""
        col = _match_column_by_name(table, col_hint)
        if col is None:
            raise UnsupportedOperationError(f"Не найден столбец «{col_hint}» для сортировки.")
        ops.append(
            PlannedOperation(
                OP_SORT,
                {"column": col.source_name, "descending": _has_stem(direction, _SORT_DESC_STEMS)},
            )
        )


def _match_column_by_name(table: TableDescriptor, hint: str) -> ColumnDescriptor | None:
    hint_norm = _norm(hint)
    if not hint_norm:
        return None
    for c in table.columns:
        if _norm(c.source_name) == hint_norm or _norm(c.normalized_name) == hint_norm:
            return c
    for c in table.columns:
        if hint_norm in _norm(c.source_name) or hint_norm in _norm(c.normalized_name):
            return c
    return None


def _compile_dedup(text: str, ops: list[PlannedOperation]) -> None:
    if _has_stem(text, _DEDUP_STEMS):
        remove = _has_stem(text, ("удали", "убери", "remove duplicate", "delete duplicate"))
        ops.append(PlannedOperation(OP_DEDUP, {"remove": remove}))


_ADD_COLUMN_RE = re.compile(
    r"(?:добавь|добавить|add)\s+(?:колонку|столбец|column)\s+([\wа-яА-ЯёЁ %_-]+?)"
    r"(?:\s*=\s*|\s+)(.+)$",
    re.I,
)
_REMOVE_COLUMN_RE = re.compile(
    r"(?:удали|удалить|remove|delete)\s+(?:колонку|столбец|column)\s+([\wа-яА-ЯёЁ %_-]+)",
    re.I,
)
_RENAME_COLUMN_RE = re.compile(
    r"(?:переименуй|переименовать|rename)\s+(?:колонку|столбец|column)?\s*([\wа-яА-ЯёЁ %_-]+?)\s+"
    r"(?:в|to)\s+([\wа-яА-ЯёЁ %_-]+)",
    re.I,
)


def _compile_column_edits(table: TableDescriptor, text: str, ops: list[PlannedOperation]) -> None:
    rm = _REMOVE_COLUMN_RE.search(text)
    if rm:
        col = _match_column_by_name(table, rm.group(1))
        if col is None:
            raise UnsupportedOperationError(f"Не найден столбец «{rm.group(1).strip()}» для удаления.")
        ops.append(PlannedOperation(OP_REMOVE_COLUMN, {"column": col.source_name}))
        return
    ren = _RENAME_COLUMN_RE.search(text)
    if ren:
        col = _match_column_by_name(table, ren.group(1))
        if col is None:
            raise UnsupportedOperationError(f"Не найден столбец «{ren.group(1).strip()}» для переименования.")
        ops.append(
            PlannedOperation(
                OP_RENAME_COLUMN, {"column": col.source_name, "new_name": ren.group(2).strip()}
            )
        )
        return
    add = _ADD_COLUMN_RE.search(text)
    if add:
        new_name = add.group(1).strip()
        rest = add.group(2).strip()
        pct_match = _PERCENT_RE.search(_norm(rest))
        if pct_match:
            pct_value = Decimal(pct_match.group(2).replace(",", "."))
            sign_word = (pct_match.group(1) or "").strip().lower()
            negative = sign_word in {"минус", "decrease", "-"}
            signed_pct = -pct_value if negative else pct_value
            col = _resolve_price_column(table, text)
            ops.append(
                PlannedOperation(
                    OP_ADD_COLUMN_PERCENT,
                    {
                        "source_column": col.source_name,
                        "new_column": new_name,
                        "percent": str(signed_pct),
                    },
                )
            )


# ---------------------------------------------------------------------------
# Compound scoped-rule contract (business-task-ownership/workset-
# continuation defect closure, PR #90 correction).
#
# Unlike every operation above, a COMPOUND request that assigns two or
# more DIFFERENT transformations to two or more DIFFERENT, non-overlapping
# row scopes in the SAME turn (e.g. "first three rows +7%, the rest +15%")
# cannot be safely recognized by ``compile_request``'s bounded regex/stem
# compiler without an ever-growing, wording-specific dictionary (ordinals,
# "the rest", ...) -- exactly the "finite dictionary of user wording" this
# project's architecture forbids. The MODEL is the correct place to
# interpret that free text into a STRUCTURED shape (see the separate,
# sanctioned managed conversational agent's own ``apply_scoped_price_
# rules`` tool -- an ordinary typed function-calling tool the model
# fills in from free text, never a second language router). This
# module's job is UNCHANGED: validate that
# structure against a small, explicit, non-extensible whitelist of
# operation/column/scope shapes BEFORE any arithmetic ever runs, exactly
# like ``compile_request`` already does for a flat operation list --
# never evaluate an expression, never accept an arbitrary column name,
# never accept an operation type outside this whitelist.
# ---------------------------------------------------------------------------

SCOPE_ROW_POSITION_RANGE = "row_position_range"
SCOPE_TEXT_CONTAINS = "text_contains"
SCOPE_PRICE_COMPARE = "price_compare"
SCOPE_REMAINDER = "remainder"
_SCOPE_KINDS = (SCOPE_ROW_POSITION_RANGE, SCOPE_TEXT_CONTAINS, SCOPE_PRICE_COMPARE, SCOPE_REMAINDER)
_COMPARE_OPERATORS = ("gt", "gte", "lt", "lte", "eq")

# The SAME field-name vocabulary the managed conversational agent's
# existing row-projection tools already expose to the model (its own
# row-field projection helper's ``role_to_key``) -- never a second
# naming scheme, and never a raw
# spreadsheet column header (which varies per uploaded file and the model
# is never asked to know).
PRICE_FIELD_ROLES = {
    "retail_price": ROLE_SELLING_PRICE,
    "purchase_price": ROLE_PURCHASE_PRICE,
    "price": ROLE_PRICE,
}
TEXT_FIELD_ROLES = {
    "brand": ROLE_BRAND,
    "category": ROLE_CATEGORY,
    "name": ROLE_PRODUCT_NAME,
}


def _resolve_role_column(table: TableDescriptor, role: str, *, field_label: str) -> ColumnDescriptor:
    col = next((c for c in table.columns if c.semantic_role == role), None)
    if col is None:
        raise UnsupportedOperationError(f"В таблице не найден столбец «{field_label}».")
    return col


def validate_scoped_price_rules(raw_rules: Any, table: TableDescriptor) -> list[dict]:
    """Validates an untrusted, model-supplied list of compound scoped
    price-adjustment rules against ``table``'s ACTUAL schema and the small,
    fixed set of shapes declared above, resolving each rule's semantic
    field name to its real column ``source_name``. Never evaluates an
    expression, never accepts a raw column name from the caller, never
    accepts a scope/operation shape outside ``_SCOPE_KINDS``/
    ``_COMPARE_OPERATORS``. Raises ``UnsupportedOperationError`` for any
    structurally invalid, unresolvable, or empty rule set -- the caller
    must then decline (never guess) exactly like every other
    ``UnsupportedOperationError`` site in this module."""

    if not isinstance(raw_rules, (list, tuple)) or not raw_rules:
        raise UnsupportedOperationError("Не указано ни одного правила изменения цены.")

    resolved: list[dict] = []
    for raw in raw_rules:
        if not isinstance(raw, Mapping):
            raise UnsupportedOperationError("Некорректный формат правила.")
        scope_raw = raw.get("scope")
        if not isinstance(scope_raw, Mapping):
            raise UnsupportedOperationError("Не указана область применения правила.")
        kind = str(scope_raw.get("kind") or "")
        if kind not in _SCOPE_KINDS:
            raise UnsupportedOperationError(f"Неизвестная область применения правила: {kind!r}.")

        scope: dict = {"kind": kind}
        if kind == SCOPE_ROW_POSITION_RANGE:
            start = scope_raw.get("start_position")
            if start is None:
                raise UnsupportedOperationError("Не указана начальная позиция строки.")
            try:
                start_i = int(start)
            except (TypeError, ValueError) as exc:
                raise UnsupportedOperationError("Некорректная начальная позиция строки.") from exc
            if start_i < 1:
                raise UnsupportedOperationError("Начальная позиция строки должна быть не меньше 1.")
            end = scope_raw.get("end_position")
            end_i = None
            if end is not None:
                try:
                    end_i = int(end)
                except (TypeError, ValueError) as exc:
                    raise UnsupportedOperationError("Некорректная конечная позиция строки.") from exc
            scope["start_position"] = start_i
            scope["end_position"] = end_i
        elif kind == SCOPE_TEXT_CONTAINS:
            field = str(scope_raw.get("text_field") or "")
            role = TEXT_FIELD_ROLES.get(field)
            if role is None:
                raise UnsupportedOperationError(f"Неизвестное текстовое поле: {field!r}.")
            col = _resolve_role_column(table, role, field_label=field)
            needle = str(scope_raw.get("contains") or "").strip()
            if not needle:
                raise UnsupportedOperationError("Не указано значение для текстового фильтра.")
            scope["column"] = col.source_name
            scope["contains"] = needle
        elif kind == SCOPE_PRICE_COMPARE:
            field = str(scope_raw.get("price_field") or "")
            role = PRICE_FIELD_ROLES.get(field)
            if role is None:
                raise UnsupportedOperationError(f"Неизвестное поле цены: {field!r}.")
            col = _resolve_role_column(table, role, field_label=field)
            operator = str(scope_raw.get("operator") or "")
            if operator not in _COMPARE_OPERATORS:
                raise UnsupportedOperationError(f"Неизвестный оператор сравнения: {operator!r}.")
            threshold = scope_raw.get("threshold")
            if threshold is None:
                raise UnsupportedOperationError("Не указано пороговое значение цены.")
            try:
                Decimal(str(threshold))
            except InvalidOperation as exc:
                raise UnsupportedOperationError("Некорректное пороговое значение цены.") from exc
            scope["column"] = col.source_name
            scope["operator"] = operator
            scope["threshold"] = str(threshold)
        # SCOPE_REMAINDER needs no further parameters.

        price_field = str(raw.get("price_field") or "retail_price")
        price_role = PRICE_FIELD_ROLES.get(price_field)
        if price_role is None:
            raise UnsupportedOperationError(f"Неизвестное поле цены: {price_field!r}.")
        price_col = _resolve_role_column(table, price_role, field_label=price_field)

        percent_raw = raw.get("percent")
        if percent_raw is None:
            raise UnsupportedOperationError("Не указан процент изменения цены.")
        try:
            percent = Decimal(str(percent_raw))
        except InvalidOperation as exc:
            raise UnsupportedOperationError("Некорректное значение процента.") from exc

        resolved.append({"scope": scope, "column": price_col.source_name, "percent": str(percent)})

    return resolved


def wants_workbook_export(text: str) -> bool:
    return _has_stem(text, _SAVE_STEMS)


def compile_request(text: str, table: TableDescriptor) -> OperationPlan:
    """Compile bounded natural-language request into a validated OperationPlan.

    Raises ``AmbiguousOperationError`` when the request cannot be uniquely
    resolved against the schema, and ``UnsupportedOperationError`` when no
    recognized operation pattern matches at all.
    """

    ops: list[PlannedOperation] = []
    _compile_dedup(text, ops)

    # Column-edit requests (add/remove/rename) are self-contained: a percent
    # expression inside "добавь колонку X +20%" describes the NEW column's
    # formula, not an in-place transform of the source price column, so it
    # must not also be compiled as a standalone OP_PERCENT_ROUND below.
    column_edit_ops: list[PlannedOperation] = []
    _compile_column_edits(table, text, column_edit_ops)
    if column_edit_ops:
        ops.extend(column_edit_ops)
    else:
        _compile_keep_only(table, text, ops)
        _compile_price_filters(table, text, ops)
        _compile_percent_round(table, text, ops)
        _compile_top_n(table, text, ops)

    wants_workbook = wants_workbook_export(text)
    if not ops and not wants_workbook:
        raise UnsupportedOperationError()
    return OperationPlan(
        operations=tuple(ops),
        wants_workbook=wants_workbook,
        raw_text=text,
        intent_summary=", ".join(o.op for o in ops) or "generate_workbook",
    )
