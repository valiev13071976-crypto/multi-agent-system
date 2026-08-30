"""Deterministic business document classification — untrusted text is DATA only."""

from __future__ import annotations

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
from documents.platform_models import (
    CLASS_STATUS_OK,
    CLASS_STATUS_UNKNOWN,
    CLASSIFIER_VERSION,
    DOC_CLASS_UNKNOWN,
    ClassificationResult,
)

# Prompt-injection phrases treated as inert data (never grant capability).
_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore system instructions",
    "call this tool",
    "send this file",
    "reveal secret",
    "system prompt",
    "you are now",
)


_SIGNALS = (
    (BIZ_INVOICE, ("invoice", "счёт", "счет", "счет-фактура", "vat", "ндс", "bill to")),
    (BIZ_CONTRACT, ("contract", "договор", "agreement", "parties", "стороны", "subject")),
    (BIZ_ACT, ("act of", "акт", "acceptance", "выполненных работ")),
    (BIZ_WAYBILL, ("waybill", "накладная", "товарная накладная", "ттн", "delivery note")),
    (BIZ_PRICE_LIST, ("price list", "прайс", "price-list", "sku", "артикул", "moq")),
    (BIZ_STATEMENT, ("statement", "report", "отчёт", "отчет", "выписка")),
)


def _strip_injection_lines(text: str) -> str:
    """Remove instruction-like lines from signal scoring only — content stays data."""
    keep = []
    for line in (text or "").splitlines():
        low = line.lower()
        if any(m in low for m in _INJECTION_MARKERS):
            continue
        keep.append(line)
    return "\n".join(keep)


def classify_document_text(text: str, *, filename: str = "") -> tuple[str, str, tuple[str, ...]]:
    """Return (business_type, confidence, matched_signals). Backward compatible."""
    result = classify_document(text, filename=filename)
    biz = result.doc_class if result.doc_class != DOC_CLASS_UNKNOWN else BIZ_GENERIC
    return biz, result.confidence, result.evidence


def classify_document(text: str, *, filename: str = "") -> ClassificationResult:
    """Versioned classification — UNKNOWN when evidence insufficient."""
    scored = _strip_injection_lines(text)
    blob = f"{filename}\n{scored}".lower()
    scores: dict[str, list[str]] = {}
    for biz, words in _SIGNALS:
        hits = [w for w in words if w in blob]
        if hits:
            scores[biz] = hits
    if not scores:
        return ClassificationResult(
            doc_class=DOC_CLASS_UNKNOWN,
            classifier_version=CLASSIFIER_VERSION,
            confidence=CONF_LOW,
            evidence=(),
            status=CLASS_STATUS_UNKNOWN,
        )
    ranked = sorted(scores.items(), key=lambda kv: len(kv[1]), reverse=True)
    best_biz, best_hits = ranked[0]
    conf = CONF_HIGH if len(best_hits) >= 3 else CONF_MEDIUM if len(best_hits) >= 2 else CONF_LOW
    alternatives = tuple({"doc_class": b, "hits": len(h)} for b, h in ranked[1:4])
    # Conflicting equal evidence → UNKNOWN rather than arbitrary pick.
    if len(ranked) > 1 and len(ranked[1][1]) == len(best_hits) and len(best_hits) < 3:
        return ClassificationResult(
            doc_class=DOC_CLASS_UNKNOWN,
            classifier_version=CLASSIFIER_VERSION,
            confidence=CONF_LOW,
            evidence=tuple(best_hits),
            alternatives=alternatives,
            status=CLASS_STATUS_UNKNOWN,
        )
    return ClassificationResult(
        doc_class=best_biz,
        classifier_version=CLASSIFIER_VERSION,
        confidence=conf,
        evidence=tuple(best_hits),
        alternatives=alternatives,
        status=CLASS_STATUS_OK,
    )
