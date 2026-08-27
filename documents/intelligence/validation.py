"""Structured document validation."""

from __future__ import annotations

from dataclasses import dataclass

from documents.intelligence.contracts import (
    BIZ_CONTRACT,
    BIZ_INVOICE,
    BIZ_PRICE_LIST,
    StructuredDocument,
)


@dataclass(frozen=True)
class ValidationOutcome:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def validate_structured(doc: StructuredDocument) -> ValidationOutcome:
    errors: list[str] = []
    warnings: list[str] = []
    amounts = dict(doc.amounts)
    identifiers = dict(doc.identifiers)

    if doc.document_type == BIZ_INVOICE:
        if not identifiers.get("invoice_number"):
            warnings.append("missing_invoice_number")
        total = amounts.get("total")
        subtotal = amounts.get("subtotal")
        vat = amounts.get("vat_amount")
        if isinstance(total, (int, float)) and isinstance(subtotal, (int, float)) and isinstance(vat, (int, float)):
            if abs(float(total) - (float(subtotal) + float(vat))) > 0.05:
                errors.append("totals_inconsistent")
        if isinstance(total, (int, float)) and total < 0:
            errors.append("negative_total")
        # line sum check
        if doc.line_items and isinstance(total, (int, float)):
            s = 0.0
            ok = True
            for item in doc.line_items:
                try:
                    qty = float(item.get("quantity") or 1)
                    price = float(item.get("price") or 0)
                    s += qty * price
                except (TypeError, ValueError):
                    ok = False
                    break
            if ok and abs(s - float(total)) > 0.5 and s > 0:
                warnings.append("line_sum_differs_from_total")

    if doc.document_type == BIZ_CONTRACT:
        if not identifiers.get("contract_number"):
            warnings.append("missing_contract_number")
        if not doc.parties:
            warnings.append("missing_parties")

    if doc.document_type == BIZ_PRICE_LIST:
        if not doc.line_items:
            warnings.append("missing_price_rows")
        for item in doc.line_items:
            if "price" in item:
                try:
                    if float(item["price"]) < 0:
                        errors.append("negative_price")
                        break
                except (TypeError, ValueError):
                    errors.append("malformed_price")
                    break

    for key, val in amounts.items():
        if isinstance(val, (int, float)) and val < 0 and key != "vat_rate":
            errors.append(f"negative_amount:{key}")

    return ValidationOutcome(ok=not errors, errors=tuple(errors), warnings=tuple(warnings))
