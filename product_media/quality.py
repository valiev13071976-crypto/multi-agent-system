"""Deterministic media quality analysis."""

from __future__ import annotations

import uuid

from product_media.errors import MEDIA_TARGET_PROFILE_VIOLATION
from product_media.platform_models import (
    ImageMetadata,
    MediaQualityIssue,
    MediaQualityReport,
    MediaQualityResult,
    QUALITY_PROFILE_VERSION,
    RIGHTS_UNKNOWN,
)
from product_media.profiles import get_target_profile
from security.tenant import require_tenant_id

# Legacy named profiles retained for Block 10 compatibility
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
    issues: list[MediaQualityIssue] = []
    measurements: dict = {
        "width": metadata.width,
        "height": metadata.height,
        "aspect_ratio": metadata.aspect_ratio,
        "profile": profile,
    }
    try:
        target = get_target_profile(profile)
        measurements["target_profile_id"] = target.profile_id
        measurements["target_profile_version"] = target.version
        measurements["source_of_rules"] = target.source_of_rules
        if metadata.width < target.width or metadata.height < target.height:
            # Allow smaller if legacy profile name used with looser mins
            if profile in _PROFILES:
                rules = _PROFILES[profile]
                if metadata.width < rules["min_width"] or metadata.height < rules["min_height"]:
                    issues.append(MediaQualityIssue("TOO_SMALL", "below profile minimum dimensions", "error"))
            else:
                issues.append(MediaQualityIssue("TOO_SMALL", "below target profile dimensions", "error"))
        if not target.allow_alpha and metadata.has_alpha:
            issues.append(MediaQualityIssue("TRANSPARENCY_NOT_ALLOWED", "alpha not allowed", "error"))
        expected = target.aspect_ratio
        if abs(metadata.aspect_ratio - expected) > 0.08 and profile not in _PROFILES:
            issues.append(MediaQualityIssue("INVALID_ASPECT_RATIO", "aspect ratio mismatch", "error"))
        if metadata.byte_size > target.max_bytes:
            issues.append(MediaQualityIssue("FILE_TOO_LARGE", "exceeds profile max bytes", "error"))
    except Exception:
        rules = _PROFILES.get(profile, _PROFILES["website"])
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
        measurements=measurements,
        passed=passed,
    )


def quality_result_from_report(
    report: MediaQualityReport,
    *,
    profile_id: str,
    rights_status: str = RIGHTS_UNKNOWN,
    fidelity_review_required: bool = False,
) -> MediaQualityResult:
    return MediaQualityResult(
        result_id=report.report_id,
        tenant_id=report.tenant_id,
        version_id=report.version_id,
        profile_id=profile_id,
        passed=report.passed,
        issues=report.issues,
        rights_status=rights_status,
        fidelity_review_required=fidelity_review_required,
    )


def assert_profile_compliance(report: MediaQualityReport) -> None:
    if not report.passed:
        from product_media.errors import MediaError

        raise MediaError(MEDIA_TARGET_PROFILE_VIOLATION)
