"""Assemble ProductContentPackage + readiness. Does not publish."""

from __future__ import annotations

import re
import uuid

from security.tenant import require_tenant_id

from product_content.category_policy import CategoryPolicy, resolve_category_policy
from product_content.contracts import (
    INSUFFICIENT_INPUT,
    POLICY_VERSION,
    STATUS_BLOCKED,
    STATUS_READY,
    STATUS_READY_WITH_WARNINGS,
    STATUS_REQUIRES_REVIEW,
    MediaPackage,
    ProductCard,
    ProductContentPackage,
    SeoPackage,
    content_version,
)
from product_content.errors import CONTENT_UNSUPPORTED_CLAIM

_CLAIM = re.compile(
    r"(?i)(\bip68\b|\bwaterproof\s+certified\b|\bofficial\s+dealer\b|\bbestseller\b|"
    r"\b№\s*1\b|\bnumber\s+one\b|\b5\s*stars?\b|\bmedical\s+grade\b|"
    r"\bfree\s+delivery\s+tomorrow\b|\b50%\s*off\b)"
)


def detect_unsupported_claims(row: dict, card: ProductCard) -> tuple[str, ...]:
    blobs = [
        str(row.get("marketing_copy") or ""),
        str(row.get("claims") or ""),
        card.short_description,
        card.long_description,
        " ".join(card.feature_bullets),
        card.canonical_title,
    ]
    text = " ".join(blobs)
    hits: list[str] = []
    for m in _CLAIM.finditer(text):
        token = m.group(0).casefold()
        if "ip68" in token or "waterproof" in token:
            spec = card.specifications.get("ip_rating")
            if spec is None or not spec.normalized:
                hits.append("unsupported_claim:ip_rating")
        elif "dealer" in token or "bestseller" in token or "№" in token or "number one" in token:
            hits.append(f"unsupported_claim:{token}")
        elif "star" in token:
            hits.append("unsupported_claim:rating")
        elif "medical" in token:
            hits.append("unsupported_claim:medical")
        else:
            hits.append(f"unsupported_claim:{token}")
    return tuple(dict.fromkeys(hits))


def decide_status(
    *,
    card: ProductCard,
    seo: SeoPackage,
    media: MediaPackage,
    policy: CategoryPolicy,
    unsupported: tuple[str, ...],
    extra_warnings: tuple[str, ...] = (),
) -> tuple[str, dict]:
    main_invalid = False
    has_valid_main = False
    for a in media.assets:
        if a.role == "MAIN":
            if a.validation_status == "VALID":
                has_valid_main = True
            elif a.validation_status in {"CORRUPT", "INVALID", "UNSUPPORTED_MIME"}:
                main_invalid = True
    reasons: dict = {
        "unsupported_claims": list(unsupported),
        "missing_required": list(card.missing_required),
        "missing_recommended": list(card.missing_recommended),
        "seo_issues": list(seo.issues),
        "media_issues": list(media.issues),
        "main_invalid": main_invalid,
        "has_valid_main": has_valid_main,
        "extra_warnings": list(extra_warnings),
    }
    if unsupported:
        return STATUS_BLOCKED, reasons
    if "unsupported_seo_claim" in seo.issues:
        return STATUS_BLOCKED, reasons
    if policy.require_main_image and (main_invalid or not has_valid_main):
        return STATUS_BLOCKED if main_invalid else STATUS_REQUIRES_REVIEW, reasons
    if main_invalid:
        return STATUS_REQUIRES_REVIEW, reasons
    if card.completeness == INSUFFICIENT_INPUT or card.missing_required:
        return STATUS_REQUIRES_REVIEW, reasons
    if card.missing_recommended or media.warnings or seo.warnings or seo.duplicate_slug or extra_warnings:
        return STATUS_READY_WITH_WARNINGS, reasons
    return STATUS_READY, reasons


def assemble_package(
    *,
    tenant_id: str,
    card: ProductCard,
    seo: SeoPackage,
    media: MediaPackage,
    source_row: dict,
    package_id: str | None = None,
    extra_warnings: tuple[str, ...] = (),
) -> ProductContentPackage:
    tenant = require_tenant_id(tenant_id)
    policy = resolve_category_policy(card.category)
    unsupported = detect_unsupported_claims(source_row, card)
    status, validation = decide_status(
        card=card,
        seo=seo,
        media=media,
        policy=policy,
        unsupported=unsupported,
        extra_warnings=extra_warnings,
    )
    version = content_version(
        {
            "card": card.version,
            "seo_title": seo.seo_title,
            "slug": seo.canonical_slug,
            "media": [a.checksum for a in media.assets],
            "policy": POLICY_VERSION,
        }
    )
    warnings = tuple(
        dict.fromkeys(
            list(card.warnings)
            + list(seo.warnings)
            + list(media.warnings)
            + ([f"missing_recommended:{f}" for f in card.missing_recommended])
            + list(extra_warnings)
        )
    )
    issues = tuple(dict.fromkeys(list(seo.issues) + list(media.issues) + list(unsupported)))
    provenance = {
        "card_fields": dict(card.field_provenance),
        "seo_fields": dict(seo.field_provenance),
        "media": [{"asset_id": a.asset_id, "kind": a.kind, "provenance": a.provenance} for a in media.assets],
        "generated": ("short_description", "long_description", "seo_title", "meta_description", "canonical_slug"),
        "reformatted": ("canonical_title", "feature_bullets"),
        "unknown": list(card.unknown_facts),
        "readiness": status,
        "economics_engine": card.economics_reference.get("engine"),
    }
    return ProductContentPackage(
        tenant_id=tenant,
        package_id=package_id or str(uuid.uuid4()),
        product_id=card.product_id,
        version=version,
        status=status,
        card=card,
        seo=seo,
        media=media,
        validation=validation,
        provenance=provenance,
        warnings=warnings,
        issues=issues,
        published=False,
        policy_version=POLICY_VERSION,
    )


# Keep claim constant importable for tests
UNSUPPORTED_CLAIM_CODE = CONTENT_UNSUPPORTED_CLAIM
