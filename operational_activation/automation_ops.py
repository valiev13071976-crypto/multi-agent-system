"""Operational verification helpers for scheduled/controlled automation (fixture-safe)."""

from __future__ import annotations

from typing import Any

from operational_activation.status import CLOSED, OFFLINE_VALIDATED


def scheduled_ops_checklist() -> dict[str, Any]:
    """Maps required operational properties to existing scheduled_automation modules."""
    return {
        "status": OFFLINE_VALIDATED,
        "may_close_if_internal": True,
        "verified_via": "scheduled_automation package + existing closure tests",
        "checklist": {
            "timezone_correctness": "scheduled_automation.recurrence.validate_timezone / compute_next",
            "recurrence": "scheduled_automation.recurrence",
            "one_time_schedule": "recurrence one-shot path",
            "cancellation": "disable/archive paths in service",
            "pause_resume": "service.pause / service.resume",
            "idempotency": "execution key schedule:v:scheduled_for",
            "restart_recovery": "store + dispatcher durable scan",
            "missed_execution_policy": "dispatcher miss handling",
            "concurrency": "store claim / duplicate suppression",
            "duplicate_suppression": "idempotent run status",
            "tenant_quotas": "access + policy",
            "budget_enforcement": "admission via existing budget path",
            "audit_trail": "store.append_audit",
            "owner_user_visibility": "API list/status by tenant",
        },
        "external_action": False,
        "closure_candidate": CLOSED,
    }


def controlled_ops_checklist() -> dict[str, Any]:
    return {
        "status": OFFLINE_VALIDATED,
        "may_close_if_internal": True,
        "verified_via": "controlled_automation package + existing closure tests",
        "action_classes_mapped": {
            "READ": "R0_READ_ONLY",
            "SAFE_INTERNAL_WRITE": "R1_PREPARE_ONLY / R2",
            "EXTERNAL_WRITE": "R3_EXTERNAL_BUSINESS_WRITE",
            "HIGH_IMPACT_WRITE": "R4_HIGH_IMPACT",
        },
        "decision_chain": [
            "trigger",
            "policy",
            "identity",
            "tenant",
            "entitlement",
            "capability",
            "budget",
            "risk_classification",
            "HITL_if_required",
            "idempotency",
            "execution",
            "audit",
            "recovery",
        ],
        "fail_closed": [
            "missing_policy",
            "unknown_capability",
            "missing_tenant",
            "expired_authorization",
            "missing_approval",
            "duplicate_idempotency",
            "external_provider_unavailable",
        ],
        "external_action": False,
        "closure_candidate": CLOSED,
    }
