"""Entity resolution — deterministic identifiers first, no LLM-only matching."""

from __future__ import annotations

from acquisition.identifiers import identifier_bundle, normalize_name
from acquisition.models import (
    MATCH_EXACT,
    MATCH_HIGH,
    MATCH_LOW,
    MATCH_MEDIUM,
    MATCH_UNRESOLVED,
    EntityMatchResult,
    MatchEvidence,
    ParsedRecord,
)


def _fields(record: ParsedRecord | dict) -> dict:
    if isinstance(record, ParsedRecord):
        return dict(record.fields)
    return dict(record or {})


def resolve_entities(
    left: ParsedRecord | dict,
    right: ParsedRecord | dict,
    *,
    left_id: str = "",
    right_id: str = "",
) -> EntityMatchResult:
    """Match two records. Hard ID conflicts block merge even if names are similar."""
    lf = _fields(left)
    rf = _fields(right)
    li = identifier_bundle(lf)
    ri = identifier_bundle(rf)
    left_rid = left_id or (left.record_id if isinstance(left, ParsedRecord) else "left")
    right_rid = right_id or (right.record_id if isinstance(right, ParsedRecord) else "right")

    conflicts: list[str] = []
    matched: list[str] = []

    # Hard identifier conflicts
    for key in ("ean", "gtin", "mpn"):
        if key in li and key in ri and li[key] != ri[key]:
            conflicts.append(f"{key}_mismatch")

    if conflicts:
        return EntityMatchResult(
            left_record_id=left_rid,
            right_record_id=right_rid,
            level=MATCH_UNRESOLVED,
            confidence=0.0,
            same_entity=False,
            evidence=MatchEvidence(
                method="hard_identifier_conflict",
                matched_fields=(),
                conflicts=tuple(conflicts),
                details={"left": li, "right": ri},
            ),
        )

    # Exact hard matches
    for key in ("ean", "gtin"):
        if key in li and key in ri and li[key] == ri[key]:
            matched.append(key)
            return EntityMatchResult(
                left_record_id=left_rid,
                right_record_id=right_rid,
                level=MATCH_EXACT,
                confidence=1.0,
                same_entity=True,
                evidence=MatchEvidence(
                    method="exact_ean",
                    matched_fields=tuple(matched),
                    details={"ean": li[key]},
                ),
            )

    if "mpn" in li and "mpn" in ri and li["mpn"] == ri["mpn"]:
        matched.append("mpn")
        brand_ok = li.get("brand") == ri.get("brand") if ("brand" in li and "brand" in ri) else True
        if brand_ok:
            return EntityMatchResult(
                left_record_id=left_rid,
                right_record_id=right_rid,
                level=MATCH_EXACT,
                confidence=0.95,
                same_entity=True,
                evidence=MatchEvidence(method="exact_mpn", matched_fields=("mpn",)),
            )
        return EntityMatchResult(
            left_record_id=left_rid,
            right_record_id=right_rid,
            level=MATCH_HIGH,
            confidence=0.85,
            same_entity=True,
            evidence=MatchEvidence(
                method="exact_mpn_brand_partial",
                matched_fields=("mpn",),
                conflicts=("brand_partial",),
            ),
        )

    if "sku" in li and "sku" in ri and li["sku"] == ri["sku"]:
        # Same supplier SKU alone is high only within same source context
        return EntityMatchResult(
            left_record_id=left_rid,
            right_record_id=right_rid,
            level=MATCH_HIGH,
            confidence=0.8,
            same_entity=True,
            evidence=MatchEvidence(method="exact_sku", matched_fields=("sku",)),
        )

    # Soft signals — never sole basis for merge
    soft: list[str] = []
    if li.get("brand") and li.get("brand") == ri.get("brand"):
        soft.append("brand")
    if li.get("model") and li.get("model") == ri.get("model"):
        soft.append("model")
    ln = normalize_name(lf.get("name") or lf.get("title") or "")
    rn = normalize_name(rf.get("name") or rf.get("title") or "")
    if ln and rn and ln == rn:
        soft.append("name")

    if soft == ["brand", "model"] or set(soft) >= {"brand", "model"}:
        return EntityMatchResult(
            left_record_id=left_rid,
            right_record_id=right_rid,
            level=MATCH_MEDIUM,
            confidence=0.6,
            same_entity=False,  # insufficient without hard ID
            evidence=MatchEvidence(
                method="brand_model_soft",
                matched_fields=tuple(soft),
                details={"note": "soft_match_not_merged"},
            ),
        )

    if soft:
        return EntityMatchResult(
            left_record_id=left_rid,
            right_record_id=right_rid,
            level=MATCH_LOW,
            confidence=0.35,
            same_entity=False,
            evidence=MatchEvidence(method="fuzzy_soft", matched_fields=tuple(soft)),
        )

    return EntityMatchResult(
        left_record_id=left_rid,
        right_record_id=right_rid,
        level=MATCH_UNRESOLVED,
        confidence=0.0,
        same_entity=False,
        evidence=MatchEvidence(method="no_signals", matched_fields=()),
    )
