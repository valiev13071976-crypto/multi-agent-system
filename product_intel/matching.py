"""Deterministic-first product/SKU matching (spec section 7/8).

Reuses the existing Block 5.1/5.2 identifier-hierarchy matcher
(``data_intel.product_match.match_products`` -> ``acquisition.entity.resolve_entities``)
instead of building a second matching engine. This module only translates
that engine's ``exact/high/medium/low/unresolved/conflict`` confidence
vocabulary into the Block 5.5 ``EXACT/HIGH_CONFIDENCE/AMBIGUOUS/NO_MATCH``
match-state vocabulary and applies it across a catalog (never merging on
soft signals alone).
"""

from __future__ import annotations

from data_intel.contracts import CONF_EXACT, CONF_HIGH
from data_intel.product_match import match_products

from product_intel.platform_models import (
    MATCH_STATE_AMBIGUOUS,
    MATCH_STATE_EXACT,
    MATCH_STATE_HIGH_CONFIDENCE,
    MATCH_STATE_NO_MATCH,
    ProductMatchCandidate,
    ProductMatchOutcome,
)


def _fields(product_like) -> dict:
    """Accept either a raw dict or a ``product_intel.platform_models.Product``."""
    if isinstance(product_like, dict):
        return dict(product_like)
    return {
        "product_id": getattr(product_like, "product_id", ""),
        "sku": getattr(product_like, "sku", "") or getattr(product_like, "article", ""),
        "article": getattr(product_like, "article", ""),
        "ean": getattr(product_like, "gtin", ""),
        "gtin": getattr(product_like, "gtin", ""),
        "mpn": getattr(product_like, "mpn", ""),
        "brand": getattr(product_like, "brand", ""),
        "name": getattr(product_like, "title", ""),
        "product_name": getattr(product_like, "title", ""),
    }


def _translate_state(match_result) -> str:
    if match_result.conflicts:
        return MATCH_STATE_NO_MATCH
    if match_result.same_entity and match_result.confidence == CONF_EXACT:
        return MATCH_STATE_EXACT
    if match_result.same_entity and match_result.confidence == CONF_HIGH:
        return MATCH_STATE_HIGH_CONFIDENCE
    if not match_result.same_entity and match_result.evidence.get("matched_fields"):
        return MATCH_STATE_AMBIGUOUS
    return MATCH_STATE_NO_MATCH


def match_pair(left, right) -> ProductMatchOutcome:
    """Bounded, deterministic pairwise product match. Never auto-merges soft signals."""
    lf = _fields(left)
    rf = _fields(right)
    result = match_products(
        lf,
        rf,
        left_ref=str(lf.get("product_id") or "left"),
        right_ref=str(rf.get("product_id") or "right"),
    )
    state = _translate_state(result)
    candidate = ProductMatchCandidate(
        product_id=str(rf.get("product_id") or ""),
        confidence=result.confidence,
        method=result.match_method,
        evidence=dict(result.evidence),
    )
    return ProductMatchOutcome(
        state=state,
        method=result.match_method,
        matched_product_id=candidate.product_id if state in {MATCH_STATE_EXACT, MATCH_STATE_HIGH_CONFIDENCE} else "",
        candidates=(candidate,) if state != MATCH_STATE_NO_MATCH else (),
        conflicts=result.conflicts,
        evidence=dict(result.evidence),
    )


def match_against_catalog(candidate, existing_products: list) -> ProductMatchOutcome:
    """Match one candidate record against a bounded existing catalog.

    Strong-identifier exact/high-confidence matches win immediately. If no
    strong match is found, any ambiguous (soft) signal across the catalog is
    surfaced as review candidates rather than silently merged or discarded
    (spec section 7: "If ambiguous: do not silently merge.").
    """
    best_exact = None
    ambiguous_candidates: list[ProductMatchCandidate] = []
    conflicts: list[str] = []
    for existing in existing_products:
        outcome = match_pair(candidate, existing)
        if outcome.state in {MATCH_STATE_EXACT, MATCH_STATE_HIGH_CONFIDENCE}:
            if best_exact is None or outcome.state == MATCH_STATE_EXACT:
                best_exact = outcome
                if outcome.state == MATCH_STATE_EXACT:
                    break
        elif outcome.state == MATCH_STATE_AMBIGUOUS:
            ambiguous_candidates.extend(outcome.candidates)
        elif outcome.conflicts:
            conflicts.extend(outcome.conflicts)

    if best_exact is not None:
        return best_exact
    if ambiguous_candidates:
        return ProductMatchOutcome(
            state=MATCH_STATE_AMBIGUOUS,
            method="soft_signal_review",
            candidates=tuple(ambiguous_candidates),
            conflicts=tuple(conflicts),
        )
    return ProductMatchOutcome(state=MATCH_STATE_NO_MATCH, method="no_signals", conflicts=tuple(conflicts))
