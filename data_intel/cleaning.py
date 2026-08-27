"""Safe deterministic cleaning — never mutates source silently."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_WS = re.compile(r"\s+")
_MISSING = {"", "-", "—", "n/a", "na", "null", "none", "нет", "#n/a", "#null!"}


def normalize_unicode(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def clean_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = normalize_unicode(str(value)).strip()
    text = _WS.sub(" ", text)
    if text.lower() in _MISSING:
        return None
    return text


def normalize_decimal_string(value: object | None) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    # Remove spaces (thousands), unify comma decimal
    t = text.replace("\u00a0", "").replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", t):
        t = t.replace(".", "").replace(",", ".")
    elif "," in t and "." not in t:
        t = t.replace(",", ".")
    elif t.count(",") == 1 and t.count(".") >= 1:
        # 1.234,56 European
        t = t.replace(".", "").replace(",", ".")
    try:
        d = Decimal(t)
    except (InvalidOperation, ValueError):
        return text
    return format(d, "f")


def normalize_boolean(value: object | None) -> bool | None:
    text = clean_text(value)
    if text is None:
        return None
    low = text.lower()
    if low in {"1", "true", "yes", "y", "да", "истина", "x", "✓"}:
        return True
    if low in {"0", "false", "no", "n", "нет", "ложь"}:
        return False
    return None


def normalize_date(value: object | None) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = clean_text(value)
    if text is None:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return text


def is_empty_row(values: dict) -> bool:
    return all(clean_text(v) is None for v in values.values())


def clean_row(values: dict, *, roles: dict | None = None) -> tuple[dict, dict]:
    """Return (cleaned_values, raw_values). Roles guide identifier-safe cleaning."""
    roles = roles or {}
    raw = {str(k): (None if v is None else str(v)) for k, v in values.items()}
    out: dict = {}
    for key, val in values.items():
        role = roles.get(key) or roles.get(str(key).lower()) or ""
        if role in {"inn", "kpp", "ogrn", "ean", "sku", "article", "mpn", "document_number"}:
            # Keep as cleaned text — never numeric cast
            out[key] = clean_text(val)
        elif role in {"price", "amount", "vat_amount", "stock", "quantity"}:
            out[key] = normalize_decimal_string(val)
        elif role in {"payment_date"}:
            out[key] = normalize_date(val)
        elif role == "currency":
            t = clean_text(val)
            out[key] = t.upper() if t else None
        else:
            out[key] = clean_text(val)
    return out, raw
