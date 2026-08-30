"""Deterministic media quality analysis."""

from __future__ import annotations

import uuid

from product_media.platform_models import (
    ImageMetadata,
    MediaQualityIssue,
    MediaQualityReport,
    QUALITY_PROFILE_VERSION,
)
from security.tenant import require_tenant_id

_PROFILES = {
    "website": {"min_width": 400, "min_height": 400, "allow_alpha": True},
    "marketplace": {"min_width": 1000, "min_height": 1000, "allow_alpha": False, "aspect_ratios": [(1, 1)]},
    "social": {"min_width": 600, "min_height": 600, "allow_alpha": True},
}


def analyze_quality(
    *,
    tenant_id: str,
    version_id: str,
    metadata: ImageMetadata,
    profile: str = "website",
) -> MediaQualityReport:
    tenant = require_tenant_id(tenant_id)
    rules = _PROFILES.get(profile, _PROFILES["website"])
    issues: list[MediaQualityIssue] = []
    if metadata.width < rules["min_width"] or metadata.height < rules["min_height"]:
        issues.append(MediaQualityIssue("TOO_SMALL", "below profile minimum dimensions", "error"))
    if not rules.get("allow_alpha", True) and metadata.has_alpha:
        issues.append(MediaQualityIssue("TRANSPARENCY_NOT_ALLOWED", "alpha not allowed", "error"))
    ratios = rules.get("aspect_ratios")
    if ratios:
        ok = any(abs(metadata.aspect_ratio - (w / h)) < 0.05 for w, h in ratios)
        if not ok:
            issues.append(MediaQualityIssue("INVALID_ASPECT_RATIO", "aspect ratio mismatch", "error"))
    passed = not any(i.severity == "error" for i in issues)
    return MediaQualityReport(
        report_id=str(uuid.uuid4()),
        tenant_id=tenant,
        version_id=version_id,
        profile_version=QUALITY_PROFILE_VERSION,
        issues=tuple(issues),
        measurements={
            "width": metadata.width,
            "height": metadata.height,
            "aspect_ratio": metadata.aspect_ratio,
            "profile": profile,
        },
        passed=passed,
    )
