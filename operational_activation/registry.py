"""Block 22–36 status registry — truthful granular states."""

from __future__ import annotations

from typing import Any

from operational_activation.automation_ops import controlled_ops_checklist, scheduled_ops_checklist
from operational_activation.channels import telegram_live_boundary, telegram_token_status, voice_live_boundary
from operational_activation.expansion_scale import expansion_gates, scale_decision
from operational_activation.issues import DEFAULT_ISSUE_BOARD
from operational_activation.legal_presentation import legal_status, PRESENTATION_EXPANDED, PRESENTATION_SHORT
from operational_activation.metrics_honesty import (
    bottleneck_status,
    load_harness_safety,
    metrics_instrumentation_status,
    optimization_status,
    percentile_dashboard_capability,
)
from operational_activation.pilot import pilot_readiness
from operational_activation.product_definition import product_definition
from operational_activation.status import (
    CLOSED,
    ENGINEERING_READY,
    HUMAN_APPROVAL_REQUIRED,
    METRICS_INSTRUMENTATION_READY,
    OFFLINE_VALIDATED,
    WAITING_FOR_EVIDENCE,
)


PUBLIC_ROUTES = (
    "/",
    "/product",
    "/capabilities",
    "/use-cases",
    "/plans",
    "/faq",
    "/security",
    "/contact",
    "/register",
    "/app",
    "/login",
    "/terms",
    "/privacy",
    "/personal-data",
    "/ai-disclosure",
    "/robots.txt",
    "/sitemap.xml",
)


def block_status_report(*, real_production_sample_count: int = 0) -> dict[str, Any]:
    sched = scheduled_ops_checklist()
    ctrl = controlled_ops_checklist()
    metrics = metrics_instrumentation_status(real_sample_count=real_production_sample_count)
    pct = percentile_dashboard_capability()
    load = load_harness_safety()
    bn = bottleneck_status()
    opt = optimization_status(bottleneck_proven=False)
    pilot = pilot_readiness()
    expand = expansion_gates(legal_onboarding_ready=False, pilot_criteria_passed=None)
    scale = scale_decision()
    issues = DEFAULT_ISSUE_BOARD.as_dict()
    legal = legal_status()
    product = product_definition()

    return {
        "22_telegram": {
            "status": OFFLINE_VALIDATED,
            "evidence": "telegram_interface webhook/fixture + BA path; channel entitlement gate",
            "credential_status": telegram_token_status(),
            "remaining_live_boundary": telegram_live_boundary(),
            "real_messages": 0,
        },
        "23_voice": {
            "status": OFFLINE_VALIDATED,
            "evidence": "voice_interface + STT/TTS abstractions; fake provider default",
            "remaining_live_boundary": voice_live_boundary(),
            "real_provider_calls": 0,
        },
        "24_scheduled_automations": {
            "status": CLOSED,
            "evidence": sched,
        },
        "25_controlled_automations": {
            "status": CLOSED,
            "evidence": ctrl,
        },
        "26_hitl_write": {
            "status": CLOSED,
            "evidence": "HitlWriteGovernor state machine + fingerprint binding; external execute denied",
            "real_write_count": 0,
            "write_default": "DENIED",
        },
        "27_production_metrics": {
            "status": metrics["status"],
            "real_sample": metrics["real_sample"],
            "evidence": metrics,
        },
        "28_p95_p99": {
            "status": pct["status"],
            "real_sample": pct["real_sample"],
            "evidence": pct,
        },
        "29_real_load": {
            "status": load["status"] if not load["production_load_executed"] else HUMAN_APPROVAL_REQUIRED,
            "production_load_executed": False,
            "evidence": load,
        },
        "30_proven_bottleneck": {
            "status": bn["status"],
            "bottleneck": bn["bottleneck"],
            "evidence": bn,
        },
        "31_optimization": {
            "status": opt["status"],
            "optimization_executed": opt["optimization_executed"],
            "evidence": opt,
        },
        "32_limited_pilot": {
            "status": pilot["status"],
            "real_users_count": pilot["real_users_invited"],
            "evidence": pilot,
        },
        "33_ux_business_issues": {
            "status": issues["status"],
            "real_issues_count": issues["real_issues_count"],
            "real_fixes_count": issues["real_fixes_count"],
            "evidence": issues,
        },
        "34_expansion": {
            "status": expand["status"],
            "expanded_users": expand["expanded_users"],
            "evidence": expand,
        },
        "35_scaling": {
            "status": scale["status"],
            "infra_mutation": scale["infra_mutation"],
            "evidence": scale,
        },
        "36_public_website": {
            "status": ENGINEERING_READY,
            "implemented_routes": list(PUBLIC_ROUTES),
            "product_definition_status": "CANONICAL",
            "legal_status": legal,
            "pricing_status": "PLACEHOLDER_PENDING",
            "real_assets_status": "PLACEHOLDERS_EXPLICIT",
            "seo_status": "READY",
            "analytics_status": "PREPARED_NO_EXTERNAL",
            "registration_funnel_status": "READY",
            "public_launch": "BLOCKED_LEGAL_PRICING_ASSETS",
            "product": product,
            "presentations": {"short": PRESENTATION_SHORT, "expanded": PRESENTATION_EXPANDED},
        },
        "meta": {
            "architecture_changes": False,
            "second_core_created": False,
            "waiting_for_evidence_blocks": [30, 31, 32, 33, 34],
            "human_approval_blocks": [22, 23, 29, 35],
        },
    }
