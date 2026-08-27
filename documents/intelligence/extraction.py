"""Schema-driven structured extraction from DocumentContent."""

from __future__ import annotations

import re
from dataclasses import dataclass

from documents.intelligence.classify import classify_document_text
from documents.intelligence.contracts import (
    BIZ_ACT,
    BIZ_CONTRACT,
    BIZ_GENERIC,
    BIZ_INVOICE,
    BIZ_PRICE_LIST,
    BIZ_WAYBILL,
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    DocumentContent,
    ExtractedField,
    StructuredDocument,
)
from documents.intelligence.validation import validate_structured


@dataclass(frozen=True)
class FieldPattern:
    name: str
    patterns: tuple[str, ...]
    group: str = "fields"  # fields|dates|amounts|identifiers|parties


def _search_patterns(text: str, patterns: tuple[str, ...]) -> str | None:
    for pat in patterns:
        m = re.search(pat, text, flags=re.I | re.M)
        if m:
            return (m.group(1) if m.lastindex else m.group(0)).strip()
    return None


CONTRACT_PATTERNS = (
    FieldPattern("contract_number", (r"(?:contract|договор)\s*(?:no|№|#)?\s*[:\s]*([A-Za-z0-9\-/]+)",), "identifiers"),
    FieldPattern("date", (r"(?:date|дата)\s*[:\s]*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",), "dates"),
    FieldPattern("amount", (r"(?:amount|сумма)\s*[:\s]*([0-9]+(?:[.,][0-9]+)?)",), "amounts"),
    FieldPattern("currency", (r"(?:currency|валюта)\s*[:\s]*([A-Z]{3}|RUB|USD|EUR|₽|\$)",), "amounts"),
    FieldPattern("subject", (r"(?:subject|предмет)\s*[:\s]*(.+)",), "fields"),
    FieldPattern("inn", (r"(?:INN|ИНН)\s*[:\s]*(\d{10,12})",), "identifiers"),
    FieldPattern("payment_terms", (r"(?:payment terms|условия оплаты)\s*[:\s]*(.+)",), "fields"),
)

INVOICE_PATTERNS = (
    FieldPattern("invoice_number", (r"(?:invoice|сч[её]т(?:-фактура)?)\s*(?:no|№|#)?\s*[:\s]*([A-Za-z0-9\-/]+)",), "identifiers"),
    FieldPattern("date", (r"(?:date|дата)\s*[:\s]*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",), "dates"),
    FieldPattern("supplier", (r"(?:supplier|продавец|поставщик)\s*[:\s]*(.+)",), "parties"),
    FieldPattern("buyer", (r"(?:buyer|покупатель|заказчик)\s*[:\s]*(.+)",), "parties"),
    FieldPattern("inn", (r"(?:INN|ИНН)\s*[:\s]*(\d{10,12})",), "identifiers"),
    FieldPattern("subtotal", (r"(?:subtotal|сумма без ндс)\s*[:\s]*([0-9]+(?:[.,][0-9]+)?)",), "amounts"),
    FieldPattern("vat_amount", (r"(?:VAT|НДС)\s*[:\s]*([0-9]+(?:[.,][0-9]+)?)",), "amounts"),
    FieldPattern("total", (r"(?<![A-Za-zА-Яа-я])(?:total|итого)\s*[:\s]*([0-9]+(?:[.,][0-9]+)?)",), "amounts"),
    FieldPattern("currency", (r"(?:currency|валюта)\s*[:\s]*([A-Z]{3}|RUB|USD|EUR)",), "amounts"),
    FieldPattern("vat_rate", (r"(?:VAT rate|ставка ндс)\s*[:\s]*([0-9]+%?)",), "amounts"),
)

ACT_PATTERNS = (
    FieldPattern("act_number", (r"(?:act|акт)\s*(?:no|№|#)?\s*[:\s]*([A-Za-z0-9\-/]+)",), "identifiers"),
    FieldPattern("date", (r"(?:date|дата)\s*[:\s]*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",), "dates"),
    FieldPattern("related_contract", (r"(?:contract|договор)\s*(?:no|№|#)?\s*[:\s]*([A-Za-z0-9\-/]+)",), "identifiers"),
    FieldPattern("total", (r"(?:total|итого|сумма)\s*[:\s]*([0-9]+(?:[.,][0-9]+)?)",), "amounts"),
)

WAYBILL_PATTERNS = (
    FieldPattern("waybill_number", (r"(?:waybill|накладная|ттн)\s*(?:no|№|#)?\s*[:\s]*([A-Za-z0-9\-/]+)",), "identifiers"),
    FieldPattern("date", (r"(?:date|дата)\s*[:\s]*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",), "dates"),
    FieldPattern("related_invoice", (r"(?:invoice|сч[её]т)\s*(?:no|№|#)?\s*[:\s]*([A-Za-z0-9\-/]+)",), "identifiers"),
    FieldPattern("total", (r"(?:total|итого)\s*[:\s]*([0-9]+(?:[.,][0-9]+)?)",), "amounts"),
)

PRICE_LIST_PATTERNS = (
    FieldPattern("supplier", (r"(?:supplier|поставщик)\s*[:\s]*(.+)",), "parties"),
    FieldPattern("effective_date", (r"(?:effective|действует с|date)\s*[:\s]*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",), "dates"),
    FieldPattern("currency", (r"(?:currency|валюта)\s*[:\s]*([A-Z]{3}|RUB|USD|EUR)",), "amounts"),
)

_SCHEMA = {
    BIZ_CONTRACT: ("contract_v1", CONTRACT_PATTERNS),
    BIZ_INVOICE: ("invoice_v1", INVOICE_PATTERNS),
    BIZ_ACT: ("act_v1", ACT_PATTERNS),
    BIZ_WAYBILL: ("waybill_v1", WAYBILL_PATTERNS),
    BIZ_PRICE_LIST: ("price_list_v1", PRICE_LIST_PATTERNS),
}


def _parse_line_items_from_tables(tables: tuple) -> list[dict]:
    items = []
    for table in tables:
        rows = table.get("rows") if isinstance(table, dict) else getattr(table, "rows", ())
        cols = table.get("columns") if isinstance(table, dict) else getattr(table, "columns", ())
        rows = list(rows or ())
        if not rows:
            continue
        header = [str(c).lower() for c in (cols or rows[0])]
        body = rows[1:] if cols else rows[1:]
        for row in body[:500]:
            cells = list(row)
            item = {}
            for i, h in enumerate(header):
                if i >= len(cells):
                    break
                val = cells[i]
                if any(k in h for k in ("sku", "артикул", "code")):
                    item["sku"] = val
                elif any(k in h for k in ("ean", "gtin", "barcode")):
                    item["ean"] = val
                elif any(k in h for k in ("name", "title", "товар", "наимен")):
                    item["name"] = val
                elif any(k in h for k in ("price", "цена")):
                    try:
                        item["price"] = float(str(val).replace(",", ".").replace(" ", ""))
                    except ValueError:
                        item["price"] = val
                elif any(k in h for k in ("qty", "qty", "quantity", "кол")):
                    try:
                        item["quantity"] = float(str(val).replace(",", "."))
                    except ValueError:
                        item["quantity"] = val
                elif any(k in h for k in ("vat", "ндс")):
                    item["vat"] = val
                elif any(k in h for k in ("unit", "ед")):
                    item["unit"] = val
            if item:
                items.append(item)
    return items


def extract_structured(
    content: DocumentContent,
    *,
    document_type: str | None = None,
    filename: str = "",
) -> StructuredDocument:
    biz, conf, signals = classify_document_text(content.text, filename=filename)
    if document_type and document_type in _SCHEMA:
        biz = document_type
    schema_id, patterns = _SCHEMA.get(biz, ("generic_v1", ()))
    text = content.text or ""
    fields: dict = {}
    dates: dict = {}
    amounts: dict = {}
    identifiers: dict = {}
    parties: list = []
    evidence: list[ExtractedField] = []

    for fp in patterns:
        val = _search_patterns(text, fp.patterns)
        if val is None:
            continue
        evidence.append(
            ExtractedField(
                name=fp.name,
                value=val,
                source_ref="text:search",
                confidence=CONF_MEDIUM,
                method="regex",
            )
        )
        if fp.group == "dates":
            dates[fp.name] = val
        elif fp.group == "amounts":
            try:
                amounts[fp.name] = float(str(val).replace(",", ".").replace("%", ""))
            except ValueError:
                amounts[fp.name] = val
        elif fp.group == "identifiers":
            identifiers[fp.name] = val
        elif fp.group == "parties":
            parties.append({"role": fp.name, "name": val})
        else:
            fields[fp.name] = val

    line_items = _parse_line_items_from_tables(content.tables)
    if biz == BIZ_PRICE_LIST and not line_items:
        # CSV-like lines: sku,price
        for line in text.splitlines()[1:200]:
            parts = re.split(r"[,;\t]", line)
            if len(parts) >= 2 and parts[0].strip():
                item = {"sku": parts[0].strip(), "name": parts[1].strip() if len(parts) > 2 else ""}
                try:
                    item["price"] = float(parts[-1].replace(",", ".").strip())
                except ValueError:
                    pass
                line_items.append(item)

    structured = StructuredDocument(
        document_id=content.document_id,
        document_type=biz if biz in _SCHEMA or biz == BIZ_GENERIC else BIZ_GENERIC,
        schema_version=schema_id,
        fields={**fields, "classification_signals": list(signals)},
        line_items=tuple(line_items),
        parties=tuple(parties),
        dates=dates,
        amounts=amounts,
        identifiers=identifiers,
        confidence=conf if evidence or line_items else CONF_LOW,
        provenance={
            "extraction_method": content.extraction_method,
            "content_confidence": content.confidence,
            "schema": schema_id,
        },
        field_evidence=tuple(evidence),
    )
    vr = validate_structured(structured)
    from dataclasses import replace

    return replace(
        structured,
        validation_ok=vr.ok,
        validation_errors=vr.errors,
    )
