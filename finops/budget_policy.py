"""Budget policy loading and compatibility with existing FinOps limits."""

from __future__ import annotations

import os
from decimal import Decimal

from finops.budget_models import (
    BUDGET_POLICY_VERSION,
    SCOPE_AGENT,
    SCOPE_DAILY,
    SCOPE_GLOBAL,
    SCOPE_MODEL,
    SCOPE_MONTHLY,
    SCOPE_PROVIDER,
    SCOPE_TASK,
    BudgetPolicy,
)
from finops.models import BudgetLimits
from finops.service import parse_decimal


def policies_from_limits(limits: BudgetLimits) -> tuple[BudgetPolicy, ...]:
    rows: list[BudgetPolicy] = []
    if limits.per_task is not None:
        rows.append(
            BudgetPolicy(
                policy_id="compat_task",
                scope=SCOPE_TASK,
                hard_limit=limits.per_task,
                window="task",
            )
        )
    if limits.per_day is not None:
        rows.append(
            BudgetPolicy(
                policy_id="compat_daily",
                scope=SCOPE_DAILY,
                hard_limit=limits.per_day,
                window="daily",
            )
        )
    if limits.per_month is not None:
        rows.append(
            BudgetPolicy(
                policy_id="compat_monthly",
                scope=SCOPE_MONTHLY,
                hard_limit=limits.per_month,
                window="monthly",
            )
        )
    return tuple(rows)


def load_advanced_budget_policies(
    *,
    limits: BudgetLimits | None = None,
    env: dict | None = None,
) -> tuple[BudgetPolicy, ...]:
    """Load optional advanced limits. Unconfigured = disabled (no surprise caps)."""
    rows: list[BudgetPolicy] = []
    if limits is not None:
        rows.extend(policies_from_limits(limits))

    mapping = (
        ("FINOPS_GLOBAL_LIMIT", SCOPE_GLOBAL, "global", "compat_global"),
        ("FINOPS_PER_AGENT_LIMIT", SCOPE_AGENT, "agent", "compat_agent"),
        ("FINOPS_PER_PROVIDER_LIMIT", SCOPE_PROVIDER, "provider", "compat_provider"),
        ("FINOPS_PER_MODEL_LIMIT", SCOPE_MODEL, "model", "compat_model"),
    )
    for env_key, scope, window, policy_id in mapping:
        if env is not None:
            amount = parse_decimal(env.get(env_key))
        else:
            amount = parse_decimal(os.getenv(env_key))
        if amount is None:
            continue
        rows.append(
            BudgetPolicy(
                policy_id=policy_id,
                scope=scope,
                hard_limit=amount,
                window=window,
            )
        )

    if env is not None:
        soft = parse_decimal(env.get("FINOPS_SOFT_LIMIT"))
        degrade = parse_decimal(env.get("FINOPS_DEGRADE_THRESHOLD"))
    else:
        soft = parse_decimal(os.getenv("FINOPS_SOFT_LIMIT"))
        degrade = parse_decimal(os.getenv("FINOPS_DEGRADE_THRESHOLD"))
    if soft is not None or degrade is not None:
        rows.append(
            BudgetPolicy(
                policy_id="compat_soft_global",
                scope=SCOPE_GLOBAL,
                soft_limit=soft,
                degrade_threshold=degrade if degrade is not None else soft,
                hard_limit=None,
                window="global",
                metadata_safe={"kind": "soft"},
            )
        )
    return tuple(rows)


def budget_policy_snapshot(policies: tuple[BudgetPolicy, ...]) -> dict:
    return {
        "policy_version": BUDGET_POLICY_VERSION,
        "policies": [
            {
                "policy_id": p.policy_id,
                "scope": p.scope,
                "scope_key": p.scope_key,
                "hard_limit": str(p.hard_limit) if p.hard_limit is not None else None,
                "soft_limit": str(p.soft_limit) if p.soft_limit is not None else None,
                "degrade_threshold": str(p.degrade_threshold)
                if p.degrade_threshold is not None
                else None,
                "currency": p.currency,
                "window": p.window,
                "enabled": p.enabled,
                "version": p.version,
            }
            for p in sorted(policies, key=lambda r: (r.scope, r.policy_id))
        ],
    }


def parse_budget_guard_required(raw: str | None) -> bool:
    if raw is None or not str(raw).strip():
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}
