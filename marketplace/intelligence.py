"""Reviews + competitor matching helpers."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from statistics import median

from marketplace.models import (
    COMP_AMBIGUOUS,
    COMP_CANDIDATE,
    COMP_MATCHED,
    COMP_REJECTED,
    CompetitorPriceObservation,
    MarketplaceReview,
    MoneyAmount,
)


def analyze_review_text(text: str) -> dict:
    t = (text or "").casefold()
    topics: list[str] = []
    if any(w in t for w in ("доставк", "delivery", "курьер")):
        topics.append("delivery")
    if any(w in t for w in ("брак", "defect", "сломан", "broken")):
        topics.append("defect")
    if any(w in t for w in ("размер", "size", "fit")):
        topics.append("size_fit")
    if any(w in t for w in ("фото", "photo", "описан", "description")):
        topics.append("content_mismatch")
    sentiment = "NEUTRAL"
    if any(w in t for w in ("отлич", "great", "good", "супер")):
        sentiment = "POSITIVE"
    if any(w in t for w in ("ужас", "bad", "плохо", "не рекоменду")):
        sentiment = "NEGATIVE"
    return {"topics": tuple(topics), "sentiment": sentiment}


def normalize_review(
    *,
    tenant_id: str,
    provider: str,
    account_id: str,
    raw: dict,
) -> MarketplaceReview:
    analysis = analyze_review_text(str(raw.get("text") or ""))
    return MarketplaceReview(
        review_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        provider=provider,
        account_id=account_id,
        external_review_id=str(raw.get("external_id") or raw.get("id") or ""),
        sku_id=str(raw.get("sku_id") or ""),
        rating=int(raw.get("rating") or 0),
        text=str(raw.get("text") or ""),
        topics=analysis["topics"],
        sentiment=analysis["sentiment"],
    )


def draft_review_reply(review: MarketplaceReview) -> dict:
    return {
        "draft": f"Thank you for your feedback regarding {', '.join(review.topics) or 'your order'}.",
        "requires_governed_write": True,
        "external_applied": False,
        "delegate_copy_to": "content_intel",
    }


def match_competitor(
    *,
    our_ean: str = "",
    our_brand: str = "",
    our_model: str = "",
    candidate: dict,
) -> str:
    ean = str(candidate.get("ean") or "")
    brand = str(candidate.get("brand") or "")
    model = str(candidate.get("model") or "")
    if our_ean and ean and our_ean == ean:
        return COMP_MATCHED
    if our_brand and our_model and brand.casefold() == our_brand.casefold() and model.casefold() == our_model.casefold():
        return COMP_MATCHED
    if our_brand and brand.casefold() == our_brand.casefold() and not our_model:
        return COMP_CANDIDATE
    title = str(candidate.get("title") or "")
    if our_model and our_model.casefold() in title.casefold() and not our_ean:
        return COMP_AMBIGUOUS
    return COMP_REJECTED


def competitor_summary(
    *,
    sku_id: str,
    provider: str,
    our_price: Decimal,
    observations: list[CompetitorPriceObservation],
) -> dict:
    matched = [o for o in observations if o.match_status == COMP_MATCHED]
    if not matched:
        return {
            "sku_id": sku_id,
            "provider": provider,
            "our_price": str(our_price),
            "sample_size": 0,
            "confidence": "NONE",
            "recommendation": None,
        }
    prices = [o.competitor_price.amount for o in matched]
    return {
        "sku_id": sku_id,
        "provider": provider,
        "our_price": str(our_price),
        "min": str(min(prices)),
        "median": str(Decimal(str(median(prices)))),
        "max": str(max(prices)),
        "sample_size": len(prices),
        "confidence": "HIGH",
        "position": "ABOVE" if our_price > max(prices) else ("BELOW" if our_price < min(prices) else "MID"),
    }


def competitive_recommendation(
    *,
    our_price: Decimal,
    target_competitor: Decimal,
    minimum_allowed: Decimal | None,
    mode: str = "MATCH",
) -> dict:
    proposed = target_competitor
    if mode == "UNDERCUT_BY_AMOUNT":
        proposed = target_competitor - Decimal("1")
    if mode == "UNDERCUT_BY_PERCENT":
        proposed = (target_competitor * Decimal("0.99")).quantize(Decimal("0.01"))
    if minimum_allowed is not None and proposed < minimum_allowed:
        return {
            "proposed": str(minimum_allowed),
            "blocked_undercut": True,
            "reason": "floor_guard",
            "floor": str(minimum_allowed),
        }
    return {"proposed": str(proposed), "blocked_undercut": False, "reason": mode}
