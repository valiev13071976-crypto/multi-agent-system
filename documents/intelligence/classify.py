"""Deterministic business document type classification."""

from __future__ import annotations

import re

from documents.intelligence.contracts import (
    BIZ_ACT,
    BIZ_CONTRACT,
    BIZ_GENERIC,
    BIZ_INVOICE,
    BIZ_PRICE_LIST,
    BIZ_STATEMENT,
    BIZ_WAYBILL,
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
)


_SIGNALS = (
    (BIZ_INVOICE, ("invoice", "счёт", "счет", "счет-фактура", "vat", "ндс", "bill to")),
    (BIZ_CONTRACT, ("contract", "договор", "agreement", "parties", "стороны", "subject")),
    (BIZ_ACT, ("act of", "акт", "acceptance", "выполненных работ")),
    (BIZ_WAYBILL, ("waybill", "накладная", "товарная накладная", "ттн", "delivery note")),
    (BIZ_PRICE_LIST, ("price list", "прайс", "price-list", "sku", "артикул", "moq")),
    (BIZ_STATEMENT, ("statement", "report", "отчёт", "отчет", "выписка")),
)


def classify_document_text(text: str, *, filename: str = "") -> tuple[str, str, tuple[str, ...]]:
    """Return (business_type, confidence, matched_signals)."""
    blob = f"{filename}\n{text}".lower()
    scores: dict[str, list[str]] = {}
    for biz, words in _SIGNALS:
        hits = [w for w in words if w in blob]
        if hits:
            scores[biz] = hits
    if not scores:
        return BIZ_GENERIC, CONF_LOW, ()
    best = max(scores.items(), key=lambda kv: len(kv[1]))
    conf = CONF_HIGH if len(best[1]) >= 3 else CONF_MEDIUM if len(best[1]) >= 2 else CONF_LOW
    return best[0], conf, tuple(best[1])
