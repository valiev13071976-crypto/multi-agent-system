"""Safe Excel formula generation — injection protection mandatory."""

from __future__ import annotations

import re

from data_intel.errors import FORMULA_VALIDATION_FAILED, DataIntelError

_SAFE_FUNC = re.compile(
    r"^(SUM|SUMIF|SUMIFS|COUNT|COUNTIF|COUNTIFS|IF|IFERROR|XLOOKUP|VLOOKUP|AVERAGE|MIN|MAX|ROUND)$",
    re.I,
)
_CELL = re.compile(r"^\$?[A-Z]{1,3}\$?\d{1,7}$", re.I)
_RANGE = re.compile(r"^\$?[A-Z]{1,3}\$?\d{1,7}:\$?[A-Z]{1,3}\$?\d{1,7}$", re.I)
_INJECTION_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def sanitize_cell_text(value: object | None) -> str:
    """Prevent formula injection from untrusted text stored as values."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_INJECTION_PREFIX):
        return "'" + text
    return text


def validate_formula(formula: str) -> str:
    """Validate a formula string. Reject arbitrary/LLM-injected unsafe content."""
    f = (formula or "").strip()
    if not f.startswith("="):
        raise DataIntelError(FORMULA_VALIDATION_FAILED)
    body = f[1:].strip()
    # Disallow external refs / commands
    lowered = body.lower()
    for bad in ("cmd|", "javascript:", "http:", "https:", "dde(", "hyperlink(", "!cmd"):
        if bad in lowered:
            raise DataIntelError(FORMULA_VALIDATION_FAILED)
    # Must start with known function or simple arithmetic of cells
    m = re.match(r"^([A-Z]+)\(", body, re.I)
    if m:
        if not _SAFE_FUNC.match(m.group(1)):
            raise DataIntelError(FORMULA_VALIDATION_FAILED)
        return "=" + body
    # Simple cell/range arithmetic
    tokens = re.split(r"([+\-*/()])", body)
    for tok in tokens:
        t = tok.strip()
        if not t or t in "+-*/()":
            continue
        if _CELL.match(t) or _RANGE.match(t) or re.fullmatch(r"\d+(\.\d+)?", t):
            continue
        raise DataIntelError(FORMULA_VALIDATION_FAILED)
    return "=" + body


def formula_sum(range_a1: str) -> str:
    return validate_formula(f"=SUM({range_a1})")


def formula_sumifs(sum_range: str, *criteria_pairs: str) -> str:
    if len(criteria_pairs) % 2 != 0 or not criteria_pairs:
        raise DataIntelError(FORMULA_VALIDATION_FAILED)
    args = ",".join([sum_range, *criteria_pairs])
    return validate_formula(f"=SUMIFS({args})")


def formula_countifs(*criteria_pairs: str) -> str:
    if len(criteria_pairs) % 2 != 0 or not criteria_pairs:
        raise DataIntelError(FORMULA_VALIDATION_FAILED)
    return validate_formula(f"=COUNTIFS({','.join(criteria_pairs)})")


def formula_margin(price_cell: str, cost_cell: str) -> str:
    return validate_formula(f"=IFERROR(({price_cell}-{cost_cell})/{price_cell},\"\")")


def formula_xlookup(lookup: str, lookup_range: str, return_range: str) -> str:
    return validate_formula(f"=XLOOKUP({lookup},{lookup_range},{return_range})")
