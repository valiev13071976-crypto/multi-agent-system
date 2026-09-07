"""Canonical product normalization (spec section 6).

Reuses the existing deterministic identifier/name normalization from
``acquisition.identifiers`` (Block 5.2) instead of building a second
normalization engine. Adds only the product-catalog-specific pieces that do
not already exist: currency/price/stock/category/attribute-key normalization.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from acquisition.identifiers import (
    normalize_brand as _normalize_brand,
    normalize_ean,
    normalize_gtin,
    normalize_mpn,
    normalize_name,
    normalize_sku,
    validate_ean,
)

__all__ = [
    "normalize_sku",
    "normalize_ean",
    "normalize_gtin",
    "validate_ean",
    "normalize_mpn",
    "normalize_brand",
    "normalize_title",
    "normalize_currency",
    "normalize_price",
    "normalize_stock_quantity",
    "normalize_attribute_key",
    "normalize_attribute_value",
    "normalize_category",
    "category_path_from_source",
]

_WS = re.compile(r"\s+")
_CURRENCY_ALIASES = {
    "руб": "RUB",
    "р.": "RUB",
    "р": "RUB",
    "rub": "RUB",
    "₽": "RUB",
    "usd": "USD",
    "$": "USD",
    "eur": "EUR",
    "€": "EUR",
    "byn": "BYN",
    "kzt": "KZT",
}


def normalize_brand(value: str | None) -> str:
    return _normalize_brand(value) or ""


def normalize_title(value: str | None) -> str:
    """Deterministic title normalization for matching (whitespace + case)."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = _WS.sub(" ", raw)
    return raw.casefold()


def normalize_currency(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[raw]
    upper = raw.upper()
    if re.fullmatch(r"[A-Z]{3}", upper):
        return upper
    return ""


def _parse_decimal(value) -> Decimal | None:
    """Deterministic numeric parsing shared by price/stock normalization.

    Preserves sign -- callers decide whether a negative result is invalid
    (price) or a validation-worthy signal (stock; see spec section 15
    "negative stock where unsupported", which requires it to surface as a
    structured INVALID/WARNING, not to be silently discarded here).
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(" ", "").replace("\u00a0", "")
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    # Deterministic decimal-separator heuristic: last of ','/'.' wins as the
    # fractional separator; the other (if present) is a thousands separator.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        # Single comma with exactly 2 trailing digits looks like a decimal
        # separator (e.g. "1234,50"); otherwise treat as thousands grouping.
        parts = text.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def normalize_price(value) -> Decimal | None:
    dec = _parse_decimal(value)
    if dec is None or dec < 0:
        return None
    return dec


def normalize_stock_quantity(value) -> Decimal | None:
    return _parse_decimal(value)


def normalize_attribute_key(value: str | None) -> str:
    raw = str(value or "").strip().lower().replace("ё", "е")
    if not raw:
        return ""
    raw = re.sub(r"[^\wа-я0-9]+", "_", raw, flags=re.I)
    return raw.strip("_")


def normalize_attribute_value(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _WS.sub(" ", raw)


def normalize_category(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _WS.sub(" ", raw)


def category_path_from_source(value: str | None, *, separators: tuple[str, ...] = ("/", ">", "\\", "|")) -> tuple[str, ...]:
    raw = str(value or "").strip()
    if not raw:
        return ()
    pattern = "|".join(re.escape(s) for s in separators)
    parts = re.split(pattern, raw)
    return tuple(normalize_category(p) for p in parts if normalize_category(p))
