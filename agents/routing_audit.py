"""Immutable routing decision audit models (P0.4).

Observability-only — does not affect provider selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


REJECT_UNAVAILABLE = "unavailable"
REJECT_INACTIVE = "inactive"
REJECT_UNSUPPORTED_CATEGORY = "unsupported_category"
REJECT_CAPABILITY_MISMATCH = "capability_mismatch"
REJECT_BUDGET_DENIED = "budget_denied"
REJECT_UNKNOWN_COST_DENIED = "unknown_cost_denied"
REJECT_HEALTH_COOLDOWN = "health_cooldown"

REJECTION_REASON_CODES = frozenset(
    {
        REJECT_UNAVAILABLE,
        REJECT_INACTIVE,
        REJECT_UNSUPPORTED_CATEGORY,
        REJECT_CAPABILITY_MISMATCH,
        REJECT_BUDGET_DENIED,
        REJECT_UNKNOWN_COST_DENIED,
        REJECT_HEALTH_COOLDOWN,
    }
)


@dataclass(frozen=True)
class RoutingCandidateAudit:
    """Safe per-candidate routing audit row."""

    provider_id: str
    model_id: str = ""
    eligible: bool = False
    rejection_reason: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "provider_id", str(self.provider_id or ""))
        object.__setattr__(self, "model_id", str(self.model_id or ""))
        if self.eligible:
            object.__setattr__(self, "rejection_reason", None)
        elif self.rejection_reason is not None:
            object.__setattr__(self, "rejection_reason", str(self.rejection_reason))

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "eligible": bool(self.eligible),
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True)
class RoutingFactorSnapshot:
    """Factors already used by the current router (no new scoring)."""

    category: str | None = None
    required_capabilities: tuple[str, ...] = ()
    complexity: str | None = None
    freshness: str | None = None
    risk: str | None = None
    context_requirement: str | None = None
    routing_policy: str | None = None
    quality_class: str | None = None
    cost_class: str | None = None
    latency_class: str | None = None
    estimated_cost: str | None = None
    max_affordable_cost: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    mode: str | None = None
    health_state: str | None = None
    health_reason: str | None = None
    runtime_sample_count: int | None = None
    runtime_success_rate: float | None = None
    runtime_latency_avg_ms: float | None = None
    runtime_cost_avg: str | None = None
    runtime_stats_state: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(str(c) for c in (self.required_capabilities or ())),
        )
        # Defensive copy — never retain caller dict by reference.
        object.__setattr__(
            self,
            "extra",
            MappingProxyType(dict(self.extra or {})),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "required_capabilities": list(self.required_capabilities),
            "complexity": self.complexity,
            "freshness": self.freshness,
            "risk": self.risk,
            "context_requirement": self.context_requirement,
            "routing_policy": self.routing_policy,
            "quality_class": self.quality_class,
            "cost_class": self.cost_class,
            "latency_class": self.latency_class,
            "estimated_cost": self.estimated_cost,
            "max_affordable_cost": self.max_affordable_cost,
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "mode": self.mode,
            "health_state": self.health_state,
            "health_reason": self.health_reason,
            "runtime_sample_count": self.runtime_sample_count,
            "runtime_success_rate": self.runtime_success_rate,
            "runtime_latency_avg_ms": self.runtime_latency_avg_ms,
            "runtime_cost_avg": self.runtime_cost_avg,
            "runtime_stats_state": self.runtime_stats_state,
            "extra": dict(self.extra),
        }


EMPTY_FACTOR_SNAPSHOT = RoutingFactorSnapshot()


def _cost_str(value) -> str | None:
    if value is None:
        return None
    return str(value)


def build_factor_snapshot(
    *,
    mode: str | None = None,
    category: str | None = None,
    requirements=None,
    routing_policy: str | None = None,
    selected_provider: str | None = None,
    selected_model: str | None = None,
    quality_class: str | None = None,
    cost_class: str | None = None,
    latency_class: str | None = None,
    estimated_cost=None,
    max_affordable_cost=None,
    health_state: str | None = None,
    health_reason: str | None = None,
    runtime_sample_count: int | None = None,
    runtime_success_rate: float | None = None,
    runtime_latency_avg_ms: float | None = None,
    runtime_cost_avg=None,
    runtime_stats_state: str | None = None,
    extra: Mapping[str, object] | None = None,
) -> RoutingFactorSnapshot:
    caps = ()
    complexity = freshness = risk = context_requirement = None
    if requirements is not None:
        caps = tuple(getattr(requirements, "required_capabilities", ()) or ())
        complexity = getattr(requirements, "complexity", None)
        freshness = getattr(requirements, "freshness", None)
        risk = getattr(requirements, "risk", None)
        context_requirement = getattr(requirements, "context_requirement", None)
    return RoutingFactorSnapshot(
        category=category,
        required_capabilities=caps,
        complexity=complexity,
        freshness=freshness,
        risk=risk,
        context_requirement=context_requirement,
        routing_policy=routing_policy,
        quality_class=quality_class,
        cost_class=cost_class,
        latency_class=latency_class,
        estimated_cost=_cost_str(estimated_cost),
        max_affordable_cost=_cost_str(max_affordable_cost),
        selected_provider=selected_provider,
        selected_model=selected_model,
        mode=mode,
        health_state=health_state,
        health_reason=health_reason,
        runtime_sample_count=runtime_sample_count,
        runtime_success_rate=runtime_success_rate,
        runtime_latency_avg_ms=runtime_latency_avg_ms,
        runtime_cost_avg=_cost_str(runtime_cost_avg),
        runtime_stats_state=runtime_stats_state,
        extra=dict(extra or {}),
    )


def routing_decision_audit_metadata(
    *,
    reason: str,
    provider_ids: tuple[str, ...],
    routing_policy_version: str,
    candidates_considered: tuple[RoutingCandidateAudit, ...] = (),
    rejected_candidates: tuple[RoutingCandidateAudit, ...] = (),
    factor_snapshot: RoutingFactorSnapshot | None = None,
) -> dict[str, object]:
    """Canonical safe metadata payload for observability emit."""

    snapshot = factor_snapshot or EMPTY_FACTOR_SNAPSHOT
    return {
        "route_reason": reason,
        "capability_match": reason,
        "provider_count": len(provider_ids),
        "selected_providers": list(provider_ids),
        "routing_policy_version": routing_policy_version,
        "candidates_considered": [c.as_dict() for c in candidates_considered],
        "rejected_candidates": [c.as_dict() for c in rejected_candidates],
        "rejection_reason_codes": sorted(
            {
                c.rejection_reason
                for c in rejected_candidates
                if c.rejection_reason
            }
        ),
        "factor_snapshot": snapshot.as_dict(),
    }
