"""Legal pre-publication checklist — honesty over marketing compliance claims."""

from __future__ import annotations

from typing import Any


LEGAL_CHECKLIST: tuple[tuple[str, str, str], ...] = (
    ("operator_legal_entity", "OPEN", "Legal entity / operator identity not finalized in product copy"),
    ("jurisdiction", "OPEN", "Governing jurisdiction TBD"),
    ("final_terms", "DRAFT", "Existing /terms is draft; not legal-reviewed"),
    ("final_privacy", "DRAFT", "Existing /privacy is draft; not legal-reviewed"),
    ("personal_data_wording", "DRAFT", "/personal-data present; wording pending counsel"),
    ("retention", "OPEN", "Retention periods need product + legal decision"),
    ("age_requirements", "OPEN", "Age gate / parental rules TBD"),
    ("payment_cancellation", "OPEN", "Commercial cancellation terms TBD (pricing pending)"),
    ("marketing_consent", "READY", "Marketing opt-in optional at register; default unchecked"),
    ("cookie_analytics", "OPEN", "No external analytics connected; disclosure if enabled"),
    ("cross_border_processing", "OPEN", "Cross-border processing disclosure TBD"),
    ("processor_list", "OPEN", "Sub-processor list TBD"),
    ("rf_filings", "OPEN", "RF filings/notifications if applicable — counsel required"),
)


def legal_status() -> dict[str, Any]:
    items = [{"id": i, "status": s, "note": n} for i, s, n in LEGAL_CHECKLIST]
    open_count = sum(1 for x in items if x["status"] in {"OPEN", "DRAFT"})
    return {
        "fully_legally_compliant_claim": False,
        "public_routes_present": ["/terms", "/privacy", "/personal-data", "/ai-disclosure"],
        "checklist": items,
        "unresolved_count": open_count,
        "pre_publication_blocker": open_count > 0,
        "note": "Do NOT claim FULLY LEGALLY COMPLIANT without separate legal review",
    }


PRESENTATION_SHORT = {
    "title": "Panda — short client presentation",
    "sections": [
        {"id": "problem", "content_key": "problem"},
        {"id": "panda", "content_key": "what_is"},
        {"id": "key_capabilities", "content_key": "capabilities_ready"},
        {"id": "examples", "content_key": "example_tasks"},
        {"id": "why_useful", "content_key": "who_for"},
        {"id": "safety_control", "content_key": "differentiator"},
        {"id": "how_to_start", "content_key": "register_funnel"},
    ],
}

PRESENTATION_EXPANDED = {
    "title": "Panda — business/partner presentation",
    "sections": [
        "product_architecture_business_level",
        "capability_map",
        "use_cases",
        "integrations",
        "automation_governance",
        "security_privacy",
        "commercial_model_placeholders",
        "deployment_readiness",
        "roadmap_honest",
    ],
    "note": "No unsupported metrics; PPT/PDF generation out of repo scope — structured content only",
}
