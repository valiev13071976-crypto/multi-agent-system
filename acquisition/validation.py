"""Record validation — structured results, not parser crashes."""

from __future__ import annotations

from urllib.parse import urlparse

from acquisition.identifiers import normalize_ean, validate_ean
from acquisition.models import RECORD_COMPETITOR, RECORD_PRICE, RECORD_SUPPLIER_ITEM, ValidationResult


_VALID_CURRENCIES = frozenset(
    {"USD", "EUR", "RUB", "GBP", "CNY", "JPY", "KZT", "BYN", "UAH", "TRY"}
)


def validate_record(record_type: str, fields: dict) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    data = dict(fields or {})

    ean = data.get("ean") or data.get("gtin")
    if ean:
        if not validate_ean(str(ean)):
            errors.append("invalid_ean")
        elif normalize_ean(str(ean)) is None:
            errors.append("malformed_ean")

    price = data.get("price")
    if price is not None:
        try:
            p = float(price)
            if p < 0:
                errors.append("negative_price")
            if p > 1_000_000_000:
                warnings.append("implausible_price")
        except (TypeError, ValueError):
            errors.append("malformed_price")

    currency = data.get("currency")
    if currency is not None and str(currency).upper() not in _VALID_CURRENCIES:
        errors.append("invalid_currency")

    stock = data.get("stock")
    if stock is not None:
        try:
            if float(stock) < 0:
                errors.append("negative_stock")
        except (TypeError, ValueError):
            errors.append("malformed_stock")

    moq = data.get("moq")
    if moq is not None:
        try:
            if float(moq) < 0:
                errors.append("negative_moq")
        except (TypeError, ValueError):
            errors.append("malformed_moq")

    url = data.get("url")
    if url:
        parsed = urlparse(str(url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("malformed_url")

    if record_type in {RECORD_PRICE, RECORD_SUPPLIER_ITEM}:
        if not any(
            data.get(k)
            for k in ("sku", "supplier_sku", "source_sku", "ean", "mpn", "name", "title")
        ):
            errors.append("missing_identifier_or_name")
        if price is None and record_type == RECORD_PRICE:
            warnings.append("missing_price")

    if record_type == RECORD_COMPETITOR:
        if not data.get("url") and not data.get("name") and not data.get("title"):
            errors.append("missing_competitor_identity")

    return ValidationResult(ok=not errors, errors=tuple(errors), warnings=tuple(warnings))
