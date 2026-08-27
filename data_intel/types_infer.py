"""Data type inference — identifier-safe (no float/scientific for IDs)."""

from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from data_intel.cleaning import clean_text, normalize_boolean, normalize_date, normalize_decimal_string
from data_intel.contracts import (
    TYPE_BOOLEAN,
    TYPE_CATEGORICAL,
    TYPE_CURRENCY,
    TYPE_DATE,
    TYPE_DATETIME,
    TYPE_DECIMAL,
    TYPE_IDENTIFIER,
    TYPE_INTEGER,
    TYPE_PERCENT,
    TYPE_STRING,
)
from data_intel.identifiers_ru import normalize_inn, normalize_kpp, normalize_ogrn

_ID_NAME = re.compile(
    r"(inn|инн|kpp|кпп|ogrn|огрн|ean|gtin|barcode|sku|артикул|mpn|account|счёт|счет)",
    re.I,
)


def infer_value_type(value: object | None, *, column_name: str = "") -> str:
    if value is None or clean_text(value) is None:
        return TYPE_STRING
    name = column_name or ""
    if _ID_NAME.search(name):
        return TYPE_IDENTIFIER
    text = str(value).strip()
    # Leading zeros → identifier
    if re.fullmatch(r"0\d+", text):
        return TYPE_IDENTIFIER
    if normalize_inn(text).valid or normalize_kpp(text).valid or normalize_ogrn(text).valid:
        return TYPE_IDENTIFIER
    if normalize_boolean(text) is not None and text.lower() in {
        "true",
        "false",
        "yes",
        "no",
        "да",
        "нет",
        "0",
        "1",
    }:
        # Only treat as bool when clearly boolean-ish column or short token
        if re.search(r"(flag|bool|active|enabled|да|нет)", name, re.I) or text.lower() in {
            "true",
            "false",
            "yes",
            "no",
            "да",
            "нет",
        }:
            return TYPE_BOOLEAN
    d = normalize_date(text)
    if d and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or ""):
        if "T" in text or " " in text.strip()[10:]:
            return TYPE_DATETIME
        return TYPE_DATE
    if text.endswith("%") or "percent" in name.lower() or "процент" in name.lower():
        return TYPE_PERCENT
    if re.search(r"(price|amount|сумм|цена|cost|currency|руб|usd|eur)", name, re.I):
        if normalize_decimal_string(text) is not None:
            return TYPE_CURRENCY
    dec = normalize_decimal_string(text)
    if dec is not None and dec != text:
        try:
            dval = Decimal(dec)
            if dval == dval.to_integral_value() and "." not in dec.rstrip("0").rstrip("."):
                return TYPE_INTEGER
            return TYPE_DECIMAL
        except InvalidOperation:
            pass
    if re.fullmatch(r"-?\d+", text) and not text.startswith("0"):
        return TYPE_INTEGER
    return TYPE_STRING


def infer_column_type(values: list, *, column_name: str = "") -> str:
    if _ID_NAME.search(column_name or ""):
        return TYPE_IDENTIFIER
    counted: Counter[str] = Counter()
    for v in values:
        if clean_text(v) is None:
            continue
        counted[infer_value_type(v, column_name=column_name)] += 1
    if not counted:
        return TYPE_STRING
    # Prefer identifier if any leading-zero or INN-like
    if counted[TYPE_IDENTIFIER] >= max(1, len([v for v in values if clean_text(v)]) // 3):
        return TYPE_IDENTIFIER
    top = counted.most_common(1)[0][0]
    unique = len({clean_text(v) for v in values if clean_text(v) is not None})
    non_null = sum(1 for v in values if clean_text(v) is not None)
    if top == TYPE_STRING and non_null and unique <= max(2, non_null // 5):
        return TYPE_CATEGORICAL
    return top
