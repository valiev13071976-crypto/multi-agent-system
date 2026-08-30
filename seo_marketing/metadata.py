"""Title / description / meta intelligence (12.2)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from seo_marketing.platform_models import (
    META_STATUS_DRAFT,
    META_STATUS_VALIDATED,
    MetaRecommendation,
    MetaSnapshot,
    MetaValidationResult,
    MODEL_GENERATED,
    SeoProvenance,
)

_STUFFING = re.compile(r"(.)\1{4,}")
_FORBIDDEN_CLAIMS = re.compile(r"(free delivery tomorrow|50% off|in stock now|certified warranty)", re.I)
_INJECTION = re.compile(r"(ignore\s+(all\s+)?previous|system\s*:|publish immediately|delete catalog)", re.I)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_meta_snapshot(
    *,
    tenant_id: str,
    page_id: str,
    page_version: int,
    title: str,
    description: str,
    canonical: str = "",
    robots: str = "",
    source: str = "crawl",
) -> MetaSnapshot:
    issues: list[str] = []
    if not title.strip():
        issues.append("missing_title")
    if not description.strip():
        issues.append("missing_description")
    return MetaSnapshot(
        snapshot_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        page_id=page_id,
        page_version=page_version,
        title=title,
        description=description,
        canonical=canonical,
        robots=robots,
        provenance=SeoProvenance(source=source, observed_at=_utc(), retrieved_at=_utc(), trust_level="DETERMINISTIC_OBSERVATION"),
        issues=tuple(issues),
    )


def validate_meta(
    *,
    title: str,
    description: str,
    canonical: str = "",
    robots: str = "",
    existing_titles: set[str] | None = None,
    trusted_facts: dict | None = None,
    max_title: int = 70,
    max_desc: int = 160,
) -> MetaValidationResult:
    issues: list[str] = []
    warnings: list[str] = []
    if not title.strip():
        issues.append("missing_title")
    if not description.strip():
        issues.append("missing_description")
    if len(title) > max_title:
        warnings.append("title_length_exceeded")
    if len(description) > max_desc:
        warnings.append("description_length_exceeded")
    if existing_titles and title.strip() in existing_titles:
        issues.append("duplicate_title")
    if _STUFFING.search(title) or _STUFFING.search(description):
        issues.append("keyword_stuffing")
    if _FORBIDDEN_CLAIMS.search(title) or _FORBIDDEN_CLAIMS.search(description):
        if not trusted_facts or not trusted_facts.get("supports_promo_claim"):
            issues.append("unsupported_commerce_claim")
    if canonical and not canonical.startswith(("http://", "https://")):
        issues.append("malformed_canonical")
    if "noindex" in robots.casefold() and "index" in robots.casefold():
        issues.append("robots_conflict")
    if _INJECTION.search(title) or _INJECTION.search(description):
        warnings.append("untrusted_instruction_detected")
    return MetaValidationResult(passed=not issues, issues=tuple(issues), warnings=tuple(warnings))


def generate_meta_recommendation(
    *,
    tenant_id: str,
    page_id: str,
    page_version: int,
    target_keyword: str,
    brand: str,
    product_facts: dict | None = None,
    current: MetaSnapshot | None = None,
) -> MetaRecommendation:
    facts = dict(product_facts or {})
    title = f"{target_keyword.strip()} | {brand}".strip(" |")
    desc_parts = [f"Learn about {target_keyword} from {brand}."]
    if facts.get("category"):
        desc_parts.append(f"Category: {facts['category']}.")
    description = " ".join(desc_parts)[:160]
    validation = validate_meta(
        title=title,
        description=description,
        trusted_facts=facts,
    )
    status = META_STATUS_VALIDATED if validation.passed else META_STATUS_DRAFT
    return MetaRecommendation(
        recommendation_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        page_id=page_id,
        page_version=page_version,
        title=title,
        description=description,
        target_keyword_ids=tuple(),
        validation=validation,
        status=status,
        generator_version="deterministic-v1",
        provenance=SeoProvenance(
            source="meta_generator",
            observed_at=_utc(),
            retrieved_at=_utc(),
            trust_level=MODEL_GENERATED,
        ),
    )
