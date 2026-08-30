"""Content validation — schema, brand, facts, length."""

from __future__ import annotations

import re

from content_intel.errors import CONTENT_FACT_UNSUPPORTED, CONTENT_MODEL_OUTPUT_INVALID, ContentIntelError
from content_intel.platform_models import (
    BrandProfile,
    STATUS_DRAFT,
    STATUS_NEEDS_REVIEW,
    STATUS_VALIDATED,
    ContentAssetVersion,
)


class ContentValidator:
    MAX_BODY_CHARS = 8000

    def validate_asset(
        self,
        asset: ContentAssetVersion,
        *,
        brand: BrandProfile | None = None,
        required_facts: tuple[str, ...] = (),
    ) -> ContentAssetVersion:
        errors: list[str] = []
        body = str(asset.body or "")
        if not body.strip():
            errors.append("empty_body")
        if len(body) > self.MAX_BODY_CHARS:
            errors.append("body_too_long")
        if brand is not None:
            for term in brand.forbidden_terms:
                if term and term.lower() in body.lower():
                    errors.append(f"forbidden_term:{term}")
        for fact_key in required_facts:
            if fact_key not in asset.product_facts_used and fact_key not in asset.missing_facts:
                errors.append(f"missing_fact_tracking:{fact_key}")
            if fact_key in asset.missing_facts and re.search(
                rf"(?i)\b{re.escape(fact_key)}\s*[:=]", body
            ):
                errors.append(CONTENT_FACT_UNSUPPORTED)
        invented_price = (
            "price" in asset.missing_facts
            and re.search(r"(?i)(?:\bprice\b\s*[:=]\s*[\d$€₽]|[$€₽]\s*[\d,.]+)", body)
        )
        if invented_price:
            errors.append(CONTENT_FACT_UNSUPPORTED)
        status = STATUS_VALIDATED if not errors else STATUS_NEEDS_REVIEW
        if errors and CONTENT_FACT_UNSUPPORTED in errors:
            raise ContentIntelError(CONTENT_FACT_UNSUPPORTED)
        return ContentAssetVersion(
            asset_id=asset.asset_id,
            version_id=asset.version_id,
            tenant_id=asset.tenant_id,
            project_id=asset.project_id,
            content_type=asset.content_type,
            channel=asset.channel,
            body=asset.body,
            status=status if not errors else STATUS_NEEDS_REVIEW,
            version_num=asset.version_num,
            parent_version_id=asset.parent_version_id,
            strategy_version_id=asset.strategy_version_id,
            idea_id=asset.idea_id,
            product_facts_used=asset.product_facts_used,
            missing_facts=asset.missing_facts,
            validation_errors=tuple(errors),
            provenance_kind=asset.provenance_kind,
            generation_profile=asset.generation_profile,
        )
