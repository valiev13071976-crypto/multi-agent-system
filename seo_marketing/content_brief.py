"""SEO → Content Factory brief handoff — no second writer."""

from __future__ import annotations

import re
import uuid

from seo_marketing.errors import SEO_FACT_UNSUPPORTED, SeoMarketingError
from seo_marketing.platform_models import (
    INTENT_UNKNOWN,
    PAGE_TYPE_UNKNOWN,
    SEOContentBrief,
)

_INVENTED = re.compile(
    r"(?i)\b(free delivery tomorrow|50%\s*off|certified warranty|in stock now|guaranteed #1)\b"
)


def build_seo_content_brief(
    *,
    tenant_id: str,
    site_id: str,
    target_page_id: str = "",
    page_type: str = PAGE_TYPE_UNKNOWN,
    primary_cluster_id: str = "",
    primary_keyword: str = "",
    supporting_keywords: tuple[str, ...] = (),
    intent: str = INTENT_UNKNOWN,
    title_recommendation: str = "",
    h1_recommendation: str = "",
    meta_recommendation: str = "",
    topics: tuple[str, ...] = (),
    internal_link_suggestions: tuple[dict, ...] = (),
    product_facts: dict[str, str] | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> SEOContentBrief:
    facts = dict(product_facts or {})
    for field_name, text in (
        ("title", title_recommendation),
        ("h1", h1_recommendation),
        ("meta", meta_recommendation),
    ):
        if _INVENTED.search(text or ""):
            # Allow only if matching fact key supplied
            if not any(k in facts for k in ("warranty", "discount", "stock", "shipping")):
                raise SeoMarketingError(SEO_FACT_UNSUPPORTED, f"invented_claim:{field_name}")
    return SEOContentBrief(
        brief_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        site_id=site_id,
        target_page_id=target_page_id,
        page_type=page_type,
        primary_cluster_id=primary_cluster_id,
        primary_keyword=primary_keyword,
        supporting_keywords=supporting_keywords,
        intent=intent,
        title_recommendation=title_recommendation,
        h1_recommendation=h1_recommendation,
        meta_recommendation=meta_recommendation,
        topics=topics,
        internal_link_suggestions=internal_link_suggestions,
        product_facts_refs=tuple(sorted(facts.keys())),
        evidence_refs=evidence_refs,
        constraints=("no_invented_product_facts", "content_factory_generates_copy"),
        status="DRAFT",
    )


def brief_to_content_factory_context(brief: SEOContentBrief) -> dict:
    """Compatible payload for Content Factory — SEO does not generate body copy."""
    return {
        "source": "seo_marketing",
        "brief_id": brief.brief_id,
        "tenant_id": brief.tenant_id,
        "site_id": brief.site_id,
        "objective": brief.primary_keyword
        or (brief.topics[0] if brief.topics else "seo_content"),
        "channel": "website",
        "content_type": "seo_text" if brief.page_type == "ARTICLE" else "landing_copy",
        "page_type": brief.page_type,
        "primary_keyword": brief.primary_keyword,
        "supporting_keywords": list(brief.supporting_keywords),
        "intent": brief.intent,
        "title": brief.title_recommendation,
        "h1": brief.h1_recommendation,
        "meta_description": brief.meta_recommendation,
        "product_facts_refs": list(brief.product_facts_refs),
        "evidence_refs": list(brief.evidence_refs),
        "constraints": list(brief.constraints),
        "delegate_generation_to": "content_intel",
    }
