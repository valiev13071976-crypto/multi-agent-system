"""Pilot cohort model — no real invites without human approval."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from operational_activation.status import HUMAN_APPROVAL_REQUIRED, ENGINEERING_READY


@dataclass
class PilotCohortConfig:
    cohort_id: str = "pilot-v1"
    max_users: int = 25
    access_plan: str = "trial"  # trial | complimentary | paid
    feature_allowlist: tuple[str, ...] = ("ai_assistant", "files_documents", "excel_data")
    usage_limit_requests_per_day: int = 50
    budget_limit_minor: int = 500
    abuse_limit_requests_per_hour: int = 30
    success_criteria: dict[str, Any] = field(
        default_factory=lambda: {
            # Thresholds configurable; not invented commercial targets
            "activation_rate_min": None,
            "first_successful_task_rate_min": None,
            "error_rate_max": None,
            "p95_latency_ms_max": None,
            "cost_per_user_max_minor": None,
            "support_tickets_per_user_max": None,
            "note": "Numeric targets require product decision before pilot CLOSE",
        }
    )
    onboarding_required: tuple[str, ...] = ("terms", "privacy", "ai_disclosure")
    support_path: str = "owner console + incident path (documented)"
    rollback: str = "disable account / revoke access grant"


def pilot_readiness() -> dict[str, Any]:
    cfg = PilotCohortConfig()
    return {
        "status": ENGINEERING_READY,
        "real_users_invited": 0,
        "real_invite_boundary": HUMAN_APPROVAL_REQUIRED,
        "cohort": {
            "cohort_id": cfg.cohort_id,
            "max_users": cfg.max_users,
            "access_plan": cfg.access_plan,
            "feature_allowlist": list(cfg.feature_allowlist),
            "usage_limit_requests_per_day": cfg.usage_limit_requests_per_day,
            "budget_limit_minor": cfg.budget_limit_minor,
            "abuse_limit_requests_per_hour": cfg.abuse_limit_requests_per_hour,
            "success_criteria": cfg.success_criteria,
            "onboarding_required": list(cfg.onboarding_required),
            "support_path": cfg.support_path,
            "rollback": cfg.rollback,
        },
        "gates": {
            "consent_legal": True,
            "tenant_isolation": True,
            "usage_limits": True,
            "budget_limits": True,
            "metrics_hooks": True,
            "feedback_capture": True,
            "disable_account": True,
        },
    }
