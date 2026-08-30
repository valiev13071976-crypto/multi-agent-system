"""Catalog intelligence — deterministic quality/readiness (Block 11.1)."""

from __future__ import annotations

import uuid

from commerce.product_platform.models import (
    CATALOG_PROFILE_VERSION,
    READINESS_NOT_READY,
    READINESS_READY,
    CatalogQualityIssue,
    CatalogQualityReport,
    TRUST_GENERATED,
    TRUST_TRUSTED,
)


_PROFILES = {
    "marketplace": {
        "required_fields": ("title", "sku", "brand"),
        "require_price": True,
        "require_stock": True,
        "require_primary_media": True,
    },
    "website": {
        "required_fields": ("title",),
        "require_price": False,
        "require_stock": False,
        "require_primary_media": False,
    },
}


def analyze_catalog(
    *,
    tenant_id: str,
    products: list[dict],
    prices: dict[str, bool],
    stock: dict[str, bool],
    media: dict[str, bool],
    profile: str = "marketplace",
) -> CatalogQualityReport:
    rules = _PROFILES.get(profile, _PROFILES["marketplace"])
    issues: list[CatalogQualityIssue] = []
    seen_sku: dict[str, str] = {}
    ready = 0
    for product in products:
        pid = product["product_id"]
        for field in rules["required_fields"]:
            if not str(product.get(field) or "").strip():
                issues.append(
                    CatalogQualityIssue("missing_field", f"missing {field}", product_id=pid, severity="error")
                )
        sku = str(product.get("sku") or "")
        if sku:
            if sku in seen_sku and seen_sku[sku] != pid:
                issues.append(
                    CatalogQualityIssue("duplicate_sku", "duplicate sku", product_id=pid, severity="error")
                )
            seen_sku[sku] = pid
        trust = dict(product.get("field_trust") or {})
        if trust.get("price") == TRUST_GENERATED and rules["require_price"]:
            issues.append(
                CatalogQualityIssue("generated_price", "price is generated not trusted", product_id=pid, severity="warning")
            )
        if rules["require_price"] and not prices.get(pid):
            issues.append(CatalogQualityIssue("missing_price", "no price", product_id=pid, severity="error"))
        if rules["require_stock"] and not stock.get(pid):
            issues.append(CatalogQualityIssue("missing_stock", "no stock", product_id=pid, severity="error"))
        if rules["require_primary_media"] and not media.get(pid):
            issues.append(CatalogQualityIssue("missing_media", "no primary media", product_id=pid, severity="error"))
        product_ready = not any(i.product_id == pid and i.severity == "error" for i in issues)
        if product_ready:
            ready += 1
        product["publication_readiness"] = READINESS_READY if product_ready else READINESS_NOT_READY
    return CatalogQualityReport(
        report_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        profile_version=CATALOG_PROFILE_VERSION,
        issues=tuple(issues),
        counts={"products": len(products), "ready": ready, "issues": len(issues)},
    )
