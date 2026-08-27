"""Cross-document linking foundation."""

from __future__ import annotations

from documents.intelligence.contracts import (
    CONF_EXACT,
    CONF_HIGH,
    CONF_LOW,
    CONF_MEDIUM,
    CONF_UNRESOLVED,
    DocumentLinkResult,
    StructuredDocument,
)


def link_documents(left: StructuredDocument, right: StructuredDocument) -> DocumentLinkResult:
    evidence: list[str] = []
    li = dict(left.identifiers)
    ri = dict(right.identifiers)
    la = dict(left.amounts)
    ra = dict(right.amounts)

    # Explicit number references
    for key in ("contract_number", "invoice_number", "act_number", "waybill_number"):
        if li.get(key) and ri.get(key) and li[key] == ri[key]:
            evidence.append(f"same_{key}")
        if li.get(key) and str(li[key]) in str(ri.values()):
            evidence.append(f"left_{key}_referenced")
        if ri.get(key) and str(ri[key]) in str(li.values()):
            evidence.append(f"right_{key}_referenced")

    if li.get("related_contract") and li.get("related_contract") == ri.get("contract_number"):
        evidence.append("act_links_contract")
    if ri.get("related_contract") and ri.get("related_contract") == li.get("contract_number"):
        evidence.append("act_links_contract")
    if li.get("related_invoice") and li.get("related_invoice") == ri.get("invoice_number"):
        evidence.append("waybill_links_invoice")
    if ri.get("related_invoice") and ri.get("related_invoice") == li.get("invoice_number"):
        evidence.append("waybill_links_invoice")

    if la.get("total") is not None and ra.get("total") is not None and la.get("total") == ra.get("total"):
        evidence.append("same_total")

    left_parties = {str(p.get("name", "")).lower() for p in left.parties}
    right_parties = {str(p.get("name", "")).lower() for p in right.parties}
    if left_parties & right_parties:
        evidence.append("shared_party")

    link_type = f"{left.document_type}:{right.document_type}"
    if not evidence:
        return DocumentLinkResult(
            left_ref=left.document_id,
            right_ref=right.document_id,
            link_type=link_type,
            confidence=CONF_UNRESOLVED,
            evidence=(),
            same_related=False,
        )
    if any(e.startswith("same_") for e in evidence) or "act_links_contract" in evidence or "waybill_links_invoice" in evidence:
        conf = CONF_EXACT if "same_contract_number" in evidence or "same_invoice_number" in evidence else CONF_HIGH
        return DocumentLinkResult(
            left_ref=left.document_id,
            right_ref=right.document_id,
            link_type=link_type,
            confidence=conf,
            evidence=tuple(evidence),
            same_related=True,
        )
    conf = CONF_MEDIUM if len(evidence) >= 2 else CONF_LOW
    return DocumentLinkResult(
        left_ref=left.document_id,
        right_ref=right.document_id,
        link_type=link_type,
        confidence=conf,
        evidence=tuple(evidence),
        same_related=conf in {CONF_HIGH, CONF_EXACT, CONF_MEDIUM},
    )
