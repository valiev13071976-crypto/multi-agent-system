"""Data quality summary for Excel business processing — no cross-tenant leakage."""

from __future__ import annotations

from data_intel.cleaning import clean_text, normalize_date, normalize_decimal_string
from data_intel.contracts import (
    CONF_LOW,
    CONF_UNRESOLVED,
    ROLE_ARTICLE,
    ROLE_EAN,
    ROLE_INN,
    ROLE_PRICE,
    ROLE_PURCHASE_PRICE,
    ROLE_SELLING_PRICE,
    ROLE_SKU,
    ROLE_UNKNOWN,
    ColumnDescriptor,
)
from data_intel.duplicates import find_duplicates
from data_intel.identifiers_ru import normalize_inn


MAP_FOUND = "FOUND"
MAP_MAPPED = "MAPPED"
MAP_AMBIGUOUS = "AMBIGUOUS"
MAP_MISSING = "MISSING"
MAP_UNSUPPORTED = "UNSUPPORTED"
NEEDS_USER_MAPPING = "NEEDS_USER_MAPPING"

CRITICAL_ROLES = frozenset(
    {ROLE_SKU, ROLE_ARTICLE, ROLE_EAN, ROLE_PRICE, ROLE_PURCHASE_PRICE, ROLE_SELLING_PRICE, ROLE_INN}
)


def column_mapping_status(columns: tuple[ColumnDescriptor, ...] | list[ColumnDescriptor]) -> list[dict]:
    """Report FOUND/MAPPED/AMBIGUOUS/MISSING/UNSUPPORTED per column."""
    out = []
    for c in columns:
        if c.semantic_role == ROLE_UNKNOWN:
            status = MAP_UNSUPPORTED if c.confidence == CONF_UNRESOLVED else MAP_AMBIGUOUS
        elif c.confidence in {CONF_LOW, CONF_UNRESOLVED}:
            status = MAP_AMBIGUOUS
        elif c.confidence in {"exact", "high"}:
            status = MAP_MAPPED if c.confidence != "exact" else MAP_FOUND
        else:
            status = MAP_MAPPED
        out.append(
            {
                "column": c.source_name,
                "role": c.semantic_role,
                "status": status,
                "confidence": c.confidence,
                "type": c.inferred_type,
            }
        )
    return out


def mapping_gate(
    columns: tuple[ColumnDescriptor, ...] | list[ColumnDescriptor],
    *,
    required_roles: set[str] | None = None,
) -> dict:
    """Fail-safe: ambiguous business-critical columns → NEEDS_USER_MAPPING."""
    required = required_roles or set()
    statuses = column_mapping_status(columns)
    ambiguous = [s for s in statuses if s["status"] == MAP_AMBIGUOUS]
    critical_ambiguous = [
        s
        for s in ambiguous
        if s["role"] in CRITICAL_ROLES or s["column"].lower() in {r.replace("_", "") for r in CRITICAL_ROLES}
    ]
    roles_present = {c.semantic_role for c in columns if c.semantic_role != ROLE_UNKNOWN}
    missing = sorted(r for r in required if r not in roles_present)
    needs = bool(critical_ambiguous) or bool(missing and not roles_present.intersection(required))
    # Also: if required roles missing entirely
    if missing:
        needs = True
    return {
        "status": NEEDS_USER_MAPPING if needs else "READY",
        "columns": statuses,
        "ambiguous_columns": ambiguous,
        "missing_required": missing,
        "needs_user_mapping": needs,
    }


def build_quality_report(
    *,
    rows: list[dict],
    columns: tuple[ColumnDescriptor, ...] | list[ColumnDescriptor] | None = None,
    source_file: str = "",
    source_sheet: str = "",
    business_keys: list[str] | None = None,
) -> dict:
    """Produce data-quality summary with row-level issues."""
    issues: list[dict] = []
    rows_invalid = 0
    invalid_prices = 0
    invalid_ids = 0
    invalid_dates = 0
    formula_cells = 0
    missing_required = 0
    keys = business_keys or ["sku", "article", "ean"]

    for i, row in enumerate(rows):
        ref = str(row.get("__row_ref") or f"r{i}")
        src_row = int(row.get("__source_row") or i + 1)
        row_bad = False
        for col, val in row.items():
            if str(col).startswith("__"):
                continue
            text = clean_text(val)
            if isinstance(val, str) and val[:1] in {"=", "+", "-", "@"}:
                formula_cells += 1
                issues.append(
                    {
                        "file": source_file,
                        "sheet": source_sheet or row.get("__source_sheet") or "",
                        "row": src_row,
                        "column": col,
                        "reason": "formula_like_value",
                        "severity": "warning",
                        "row_ref": ref,
                    }
                )
            role_hint = str(col).lower()
            if role_hint in {"price", "selling_price", "purchase_price", "promo_price"} or "цена" in role_hint:
                if text is not None:
                    parsed = normalize_decimal_string(val)
                    bad = False
                    if parsed is None:
                        bad = True
                    else:
                        try:
                            from decimal import Decimal, InvalidOperation

                            Decimal(parsed)
                            # If normalize returned original non-numeric text, Decimal may still fail
                        except Exception:
                            bad = True
                        else:
                            # Heuristic: reject if original has letters (except currency stripped already)
                            if any(ch.isalpha() for ch in text.replace("RUB", "").replace("USD", "").replace("EUR", "")):
                                try:
                                    Decimal(text.replace(",", ".").replace(" ", ""))
                                except Exception:
                                    bad = True
                    if bad:
                        invalid_prices += 1
                        row_bad = True
                        issues.append(
                            {
                                "file": source_file,
                                "sheet": source_sheet or row.get("__source_sheet") or "",
                                "row": src_row,
                                "column": col,
                                "reason": "invalid_price",
                                "severity": "error",
                                "row_ref": ref,
                            }
                        )
                elif role_hint in {"price", "selling_price", "purchase_price"}:
                    missing_required += 1
            if role_hint in {"inn"}:
                n = normalize_inn(val)
                if text and not n.valid:
                    invalid_ids += 1
                    row_bad = True
                    issues.append(
                        {
                            "file": source_file,
                            "sheet": source_sheet or "",
                            "row": src_row,
                            "column": col,
                            "reason": "invalid_identifier",
                            "severity": "error",
                            "row_ref": ref,
                        }
                    )
            if role_hint in {"date", "payment_date"} and text:
                d = normalize_date(val)
                if d == text and not (len(d) == 10 and d[4] == "-"):
                    # ambiguous leftover
                    invalid_dates += 1
                    issues.append(
                        {
                            "file": source_file,
                            "sheet": source_sheet or "",
                            "row": src_row,
                            "column": col,
                            "reason": "invalid_or_ambiguous_date",
                            "severity": "warning",
                            "row_ref": ref,
                        }
                    )
        if not any(clean_text(row.get(k)) for k in keys):
            missing_required += 1
            issues.append(
                {
                    "file": source_file,
                    "sheet": source_sheet or row.get("__source_sheet") or "",
                    "row": src_row,
                    "column": ",".join(keys),
                    "reason": "missing_required_values",
                    "severity": "warning",
                    "row_ref": ref,
                }
            )
        if row_bad:
            rows_invalid += 1

    dups = find_duplicates(rows, business_keys=keys)
    mapping = column_mapping_status(columns or ())
    ambiguous_cols = [m for m in mapping if m["status"] == MAP_AMBIGUOUS]

    return {
        "rows_total": len(rows),
        "rows_valid": max(0, len(rows) - rows_invalid),
        "rows_invalid": rows_invalid,
        "duplicates": len(dups),
        "duplicate_groups": dups,
        "missing_required_values": missing_required,
        "ambiguous_columns": ambiguous_cols,
        "invalid_prices": invalid_prices,
        "invalid_identifiers": invalid_ids,
        "invalid_dates": invalid_dates,
        "formula_cells": formula_cells,
        "warnings": [x for x in issues if x.get("severity") == "warning"],
        "errors": [x for x in issues if x.get("severity") == "error"],
        "issues": issues,
        "mapping": mapping,
    }
