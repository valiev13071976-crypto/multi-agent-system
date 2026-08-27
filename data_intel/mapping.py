"""Column semantic mapping — deterministic aliases first; LLM only as optional ambiguous helper."""

from __future__ import annotations

import re
from collections import Counter

from data_intel.cleaning import clean_text
from data_intel.contracts import (
    CONF_EXACT,
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    CONF_UNRESOLVED,
    ROLE_AMOUNT,
    ROLE_ARTICLE,
    ROLE_COMPANY_NAME,
    ROLE_COUNTERPARTY,
    ROLE_CURRENCY,
    ROLE_DOCUMENT_NUMBER,
    ROLE_EAN,
    ROLE_INN,
    ROLE_KPP,
    ROLE_MPN,
    ROLE_OGRN,
    ROLE_PAYMENT_DATE,
    ROLE_PRICE,
    ROLE_PRODUCT_NAME,
    ROLE_QUANTITY,
    ROLE_SKU,
    ROLE_STOCK,
    ROLE_SUPPLIER,
    ROLE_UNIT,
    ROLE_UNKNOWN,
    ROLE_VAT_AMOUNT,
    ROLE_VAT_RATE,
    ROLE_WAREHOUSE,
    ColumnDescriptor,
)
from data_intel.identifiers_ru import normalize_inn
from data_intel.types_infer import infer_column_type

_ALIAS: dict[str, str] = {
    "inn": ROLE_INN,
    "инн": ROLE_INN,
    "kpp": ROLE_KPP,
    "кпп": ROLE_KPP,
    "ogrn": ROLE_OGRN,
    "огрн": ROLE_OGRN,
    "company": ROLE_COMPANY_NAME,
    "company_name": ROLE_COMPANY_NAME,
    "название": ROLE_COMPANY_NAME,
    "наименование": ROLE_COMPANY_NAME,
    "организация": ROLE_COMPANY_NAME,
    "контрагент": ROLE_COUNTERPARTY,
    "counterparty": ROLE_COUNTERPARTY,
    "partner": ROLE_COUNTERPARTY,
    "sku": ROLE_SKU,
    "артикул": ROLE_ARTICLE,
    "article": ROLE_ARTICLE,
    "ean": ROLE_EAN,
    "gtin": ROLE_EAN,
    "barcode": ROLE_EAN,
    "штрихкод": ROLE_EAN,
    "mpn": ROLE_MPN,
    "product": ROLE_PRODUCT_NAME,
    "product_name": ROLE_PRODUCT_NAME,
    "товар": ROLE_PRODUCT_NAME,
    "номенклатура": ROLE_PRODUCT_NAME,
    "qty": ROLE_QUANTITY,
    "quantity": ROLE_QUANTITY,
    "количество": ROLE_QUANTITY,
    "кол-во": ROLE_QUANTITY,
    "unit": ROLE_UNIT,
    "ед": ROLE_UNIT,
    "price": ROLE_PRICE,
    "цена": ROLE_PRICE,
    "currency": ROLE_CURRENCY,
    "валюта": ROLE_CURRENCY,
    "stock": ROLE_STOCK,
    "остаток": ROLE_STOCK,
    "наличие": ROLE_STOCK,
    "amount": ROLE_AMOUNT,
    "сумма": ROLE_AMOUNT,
    "vat": ROLE_VAT_AMOUNT,
    "ндс": ROLE_VAT_AMOUNT,
    "vat_rate": ROLE_VAT_RATE,
    "ставка_ндс": ROLE_VAT_RATE,
    "date": ROLE_PAYMENT_DATE,
    "дата": ROLE_PAYMENT_DATE,
    "payment_date": ROLE_PAYMENT_DATE,
    "document": ROLE_DOCUMENT_NUMBER,
    "doc_number": ROLE_DOCUMENT_NUMBER,
    "номер": ROLE_DOCUMENT_NUMBER,
    "счёт": ROLE_DOCUMENT_NUMBER,
    "счет": ROLE_DOCUMENT_NUMBER,
    "warehouse": ROLE_WAREHOUSE,
    "склад": ROLE_WAREHOUSE,
    "supplier": ROLE_SUPPLIER,
    "поставщик": ROLE_SUPPLIER,
}


def _norm_header(name: str) -> str:
    text = clean_text(name) or ""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\wа-яa-z0-9]+", "_", text, flags=re.I)
    return text.strip("_")


def map_header_role(header: str) -> tuple[str, str]:
    """Return (role, confidence)."""
    key = _norm_header(header)
    if key in _ALIAS:
        return _ALIAS[key], CONF_EXACT
    for alias, role in _ALIAS.items():
        if alias in key or key in alias:
            return role, CONF_HIGH
    return ROLE_UNKNOWN, CONF_UNRESOLVED


def profile_override_role(header: str, values: list, deterministic_role: str) -> tuple[str, str]:
    """Value profile can strengthen mapping; cannot silently override deterministic contradiction."""
    role, conf = map_header_role(header)
    if role != ROLE_UNKNOWN and conf in {CONF_EXACT, CONF_HIGH}:
        return role, conf
    # Profile: many valid INNs
    inn_hits = sum(1 for v in values if normalize_inn(str(v or "")).valid)
    non_null = sum(1 for v in values if clean_text(v) is not None) or 1
    if inn_hits / non_null >= 0.6:
        if deterministic_role not in {ROLE_UNKNOWN, ROLE_INN} and deterministic_role != ROLE_INN:
            # contradiction — keep deterministic, flag unresolved confidence
            return deterministic_role, CONF_LOW
        return ROLE_INN, CONF_HIGH
    return role if role != ROLE_UNKNOWN else deterministic_role, conf if role != ROLE_UNKNOWN else CONF_MEDIUM


def map_columns(
    headers: list[str],
    rows: list[dict],
    *,
    llm_suggestions: dict[str, str] | None = None,
) -> tuple[ColumnDescriptor, ...]:
    llm_suggestions = llm_suggestions or {}
    cols: list[ColumnDescriptor] = []
    for h in headers:
        values = [r.get(h) for r in rows]
        det_role, det_conf = map_header_role(h)
        role, conf = profile_override_role(h, values, det_role)
        # Optional LLM — only if unresolved and no contradiction
        if role == ROLE_UNKNOWN and h in llm_suggestions:
            sug = llm_suggestions[h]
            if sug and sug != ROLE_UNKNOWN:
                role, conf = sug, CONF_MEDIUM
        elif role != ROLE_UNKNOWN and h in llm_suggestions:
            sug = llm_suggestions[h]
            if sug and sug != role:
                # do not override deterministic
                conf = det_conf
        inferred = infer_column_type(values, column_name=h)
        if role in {ROLE_INN, ROLE_KPP, ROLE_OGRN, ROLE_EAN, ROLE_SKU, ROLE_ARTICLE, ROLE_MPN}:
            inferred = "identifier"
        examples = tuple(
            str(v)[:40]
            for v in values
            if clean_text(v) is not None
        )[:3]
        cols.append(
            ColumnDescriptor(
                source_name=h,
                normalized_name=_norm_header(h) or h,
                inferred_type=inferred,
                semantic_role=role,
                nullable=any(clean_text(v) is None for v in values),
                confidence=conf,
                examples_safe=examples,
            )
        )
    return tuple(cols)


def role_map(columns: tuple[ColumnDescriptor, ...]) -> dict[str, str]:
    return {c.source_name: c.semantic_role for c in columns}
