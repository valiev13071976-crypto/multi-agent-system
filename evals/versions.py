"""Explicit artifact version constants + content snapshots (no source-file hashing)."""

from __future__ import annotations

from evals.models import content_hash

# Prompt / role (authoritative in agents.role_registry)
from agents.role_registry import PROMPT_VERSION, ROLE_REGISTRY_VERSION  # noqa: E402

# Routing (authoritative constant in agents.model_profile)
from agents.model_profile import ROUTING_POLICY_VERSION  # noqa: E402

# Autonomy (authoritative constant lives in autonomy.policy)
from autonomy.policy import POLICY_VERSION  # noqa: E402

# Validators / judge (authoritative on validators/judge modules)
from agents.fact_validator import VALIDATOR_VERSION as FACT_VALIDATOR_VERSION
from agents.judge import JUDGE_VERSION
from agents.validators.consistency import VALIDATOR_VERSION as CONSISTENCY_VALIDATOR_VERSION
from agents.validators.structural import VALIDATOR_VERSION as STRUCTURAL_VALIDATOR_VERSION

# Eval suite
CORE_SUITE_VERSION = "1.2.0"


def normalize_prompt_text(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def prompt_content_hash(text: str) -> str:
    return content_hash({"text": normalize_prompt_text(text)})


def policy_snapshot() -> dict:
    """Declarative snapshot of AutonomyGate policy rules (not Python source)."""
    from autonomy.models import (
        ACTION_DELETE,
        ACTION_EXECUTE_CODE,
        ACTION_EXTERNAL_PUBLISH,
        ACTION_FINANCIAL_CHANGE,
        ACTION_PERMISSION_CHANGE,
        ACTION_PURCHASE,
        ACTION_SEND_MESSAGE,
    )
    from autonomy.policy import NEVER_AUTO_ALLOW_TYPES

    return {
        "policy_version": POLICY_VERSION,
        "never_auto_allow": sorted(NEVER_AUTO_ALLOW_TYPES),
        "rules": [
            "deny_by_default",
            "privileged_or_critical_require_approval",
            "irreversible_write_require_approval",
            "bounded_low_internal_allow",
            "bounded_reversible_write_review_after",
            "read_allowed_internal_or_readonly_external",
        ],
        "never_auto_types_check": sorted(
            {
                ACTION_PURCHASE,
                ACTION_FINANCIAL_CHANGE,
                ACTION_SEND_MESSAGE,
                ACTION_EXTERNAL_PUBLISH,
                ACTION_PERMISSION_CHANGE,
                ACTION_DELETE,
                ACTION_EXECUTE_CODE,
            }
        ),
    }


def routing_policy_snapshot() -> dict:
    from agents.model_profile import (
        DEFAULT_AUTO_ROUTING_POLICY,
        POLICY_BALANCED,
        POLICY_COST,
        POLICY_LATENCY,
        POLICY_PRIORITY,
        POLICY_QUALITY,
    )

    return {
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "default_policy": DEFAULT_AUTO_ROUTING_POLICY,
        "policies": sorted(
            {
                POLICY_BALANCED,
                POLICY_COST,
                POLICY_LATENCY,
                POLICY_PRIORITY,
                POLICY_QUALITY,
            }
        ),
    }


def validator_snapshot(validator_id: str, version: str) -> dict:
    return {"validator_id": validator_id, "validator_version": version}


def judge_snapshot() -> dict:
    return {"judge_id": "Judge", "judge_version": JUDGE_VERSION}
