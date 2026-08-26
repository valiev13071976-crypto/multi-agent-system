from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from agents.capability_match import missing_capabilities, profile_satisfies_requirements
from agents.model_profile import (
    COST_RANK,
    FALLBACK_ERROR,
    FALLBACK_GENERAL,
    FALLBACK_PRIORITY,
    LATENCY_RANK,
    POLICY_BALANCED,
    POLICY_COST,
    POLICY_LATENCY,
    POLICY_PRIORITY,
    POLICY_QUALITY,
    QUALITY_RANK,
    ROUTING_POLICY_VERSION,
    balanced_score,
    routing_category_for_role,
)
from agents.provider_registry import PROVIDER_IDS, ProviderRegistry
from agents.routing_audit import (
    EMPTY_FACTOR_SNAPSHOT,
    REJECT_BUDGET_DENIED,
    REJECT_CAPABILITY_MISMATCH,
    REJECT_HEALTH_COOLDOWN,
    REJECT_UNAVAILABLE,
    REJECT_UNKNOWN_COST_DENIED,
    REJECT_UNSUPPORTED_CATEGORY,
    RoutingCandidateAudit,
    RoutingFactorSnapshot,
    build_factor_snapshot,
    routing_decision_audit_metadata,
)


REASON_EXPLICIT_PROVIDER = "explicit_provider"
REASON_ALL_AVAILABLE_PROVIDERS = "all_available_providers"
REASON_AUTO_PROVIDER = "auto_provider"
REASON_AUTO_CAPABILITY_MATCH = "auto_capability_match"
REASON_AUTO_BUDGET_MATCH = "auto_budget_match"
REASON_AUTO_GENERAL_FALLBACK = "auto_general_fallback"
REASON_AUTO_PRIORITY_FALLBACK = "auto_priority_fallback"
REASON_AUTO_REQUIREMENTS_MATCH = "auto_requirements_match"
REASON_EXPLICIT_CAPABILITY_MISMATCH = "explicit_capability_mismatch"
REASON_EXPLICIT_BUDGET_DENIED = "explicit_budget_denied"

EXPLICIT_MODES = frozenset(PROVIDER_IDS)
MODE_AUTO = "auto"
MODE_BOTH = "both"


@dataclass(frozen=True)
class RoutingDecision:
    role_id: str
    provider_ids: tuple[str, ...]
    models: Mapping[str, str]
    reason: str
    routing_policy_version: str = ROUTING_POLICY_VERSION
    candidates_considered: tuple[RoutingCandidateAudit, ...] = ()
    rejected_candidates: tuple[RoutingCandidateAudit, ...] = ()
    factor_snapshot: RoutingFactorSnapshot = field(default_factory=RoutingFactorSnapshot)

    def __post_init__(self):
        object.__setattr__(self, "provider_ids", tuple(self.provider_ids))
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))
        object.__setattr__(
            self,
            "routing_policy_version",
            self.routing_policy_version or ROUTING_POLICY_VERSION,
        )
        object.__setattr__(
            self,
            "candidates_considered",
            tuple(self.candidates_considered or ()),
        )
        object.__setattr__(
            self,
            "rejected_candidates",
            tuple(self.rejected_candidates or ()),
        )
        snapshot = self.factor_snapshot
        if snapshot is None:
            snapshot = EMPTY_FACTOR_SNAPSHOT
        elif not isinstance(snapshot, RoutingFactorSnapshot):
            snapshot = RoutingFactorSnapshot(**dict(snapshot))
        object.__setattr__(self, "factor_snapshot", snapshot)


class NoCapableProviderError(Exception):
    def __init__(
        self,
        category: str,
        *,
        reason: str = "category",
        missing_capabilities: tuple[str, ...] = (),
        candidates_considered: tuple[RoutingCandidateAudit, ...] = (),
        rejected_candidates: tuple[RoutingCandidateAudit, ...] = (),
        factor_snapshot: RoutingFactorSnapshot | None = None,
    ):
        self.category = category
        self.reason = reason
        self.missing_capabilities = tuple(missing_capabilities or ())
        self.candidates_considered = tuple(candidates_considered or ())
        self.rejected_candidates = tuple(rejected_candidates or ())
        self.factor_snapshot = factor_snapshot or EMPTY_FACTOR_SNAPSHOT
        if self.reason == "requirements":
            message = (
                f"No configured provider satisfies required capabilities "
                f"for task category {category!r}."
            )
        else:
            message = f"No configured provider supports task category {category!r}."
        super().__init__(message)


class ProviderCapabilityMismatchError(Exception):
    """Explicit provider selected but profile lacks required capabilities."""

    def __init__(
        self,
        provider: str,
        *,
        missing_capabilities: tuple[str, ...] = (),
        category: str | None = None,
        candidates_considered: tuple[RoutingCandidateAudit, ...] = (),
        rejected_candidates: tuple[RoutingCandidateAudit, ...] = (),
        factor_snapshot: RoutingFactorSnapshot | None = None,
    ):
        self.provider = provider
        self.missing_capabilities = tuple(missing_capabilities or ())
        self.category = category
        self.candidates_considered = tuple(candidates_considered or ())
        self.rejected_candidates = tuple(rejected_candidates or ())
        self.factor_snapshot = factor_snapshot or EMPTY_FACTOR_SNAPSHOT
        super().__init__(
            f"Provider {provider!r} does not satisfy required capabilities."
        )


class BudgetRoutingDeniedError(Exception):
    """Capable candidate(s) exist but none satisfy routing budget constraints."""

    def __init__(
        self,
        reason: str = "budget_no_affordable_capable_route",
        *,
        provider: str | None = None,
        category: str | None = None,
        candidates_considered: tuple[RoutingCandidateAudit, ...] = (),
        rejected_candidates: tuple[RoutingCandidateAudit, ...] = (),
        factor_snapshot: RoutingFactorSnapshot | None = None,
    ):
        self.reason = reason
        self.provider = provider
        self.category = category
        self.candidates_considered = tuple(candidates_considered or ())
        self.rejected_candidates = tuple(rejected_candidates or ())
        self.factor_snapshot = factor_snapshot or EMPTY_FACTOR_SNAPSHOT
        super().__init__(reason)


class ModelRouter:
    """
    Formalizes mode → provider selection.
    Does not inspect the user prompt or classify tasks.

    ``mode=both`` intentionally does not apply TaskRequirements capability
    filtering, routing-time budget filtering, or dynamic health filtering
    (preserves multi-provider fan-out; execution-time BudgetGuard remains
    authoritative). Capability, budget, and health eligibility apply to
    ``auto``; explicit mode validates capability/budget without silent
    re-route and does not exclude an explicit provider for cooldown.

    P0.4 audit fields on RoutingDecision are observability-only and must not
    change which provider is selected. P1.1 health excludes cooldown
    providers from ``auto`` only.
    """

    def __init__(self, registry: ProviderRegistry, health_tracker=None):
        self.registry = registry
        self.observability = None
        self.health_tracker = health_tracker

    def _model_id(self, provider_id: str) -> str:
        try:
            return str(self.registry.model(provider_id) or "")
        except Exception:
            return ""

    def _health_snapshot(self, provider_id: str):
        tracker = self.health_tracker
        if tracker is None or not getattr(getattr(tracker, "policy", None), "enabled", True):
            return None
        try:
            return tracker.snapshot(provider_id, self._model_id(provider_id))
        except Exception:
            return None

    def _filter_by_health(self, provider_ids: tuple[str, ...]) -> tuple[str, ...]:
        tracker = self.health_tracker
        if tracker is None or not getattr(getattr(tracker, "policy", None), "enabled", True):
            return tuple(provider_ids)
        kept = []
        for provider_id in provider_ids:
            try:
                if tracker.is_auto_eligible(provider_id, self._model_id(provider_id)):
                    kept.append(provider_id)
            except Exception:
                # Fail open on tracker errors — unknown/broken health ≠ unhealthy.
                kept.append(provider_id)
        return tuple(kept)

    def _health_blocked_set(self, provider_ids: tuple[str, ...]) -> set[str]:
        eligible = set(self._filter_by_health(provider_ids))
        return {p for p in provider_ids if p not in eligible}

    def _budget_reject_reason(self, provider_id: str, budget_constraints) -> str:
        costs = dict(getattr(budget_constraints, "candidate_costs", None) or {})
        if provider_id in costs and costs[provider_id] is None:
            return REJECT_UNKNOWN_COST_DENIED
        return REJECT_BUDGET_DENIED

    def _factor_for(
        self,
        *,
        mode: str,
        role_id: str,
        category: str | None,
        requirements,
        budget_constraints,
        selected: tuple[str, ...],
    ) -> RoutingFactorSnapshot:
        selected_provider = selected[0] if selected else None
        selected_model = (
            self._model_id(selected_provider) if selected_provider else None
        )
        quality = cost = latency = None
        estimated = None
        if selected_provider:
            profile = self.registry.profile(selected_provider)
            if profile is not None:
                quality = profile.quality_class
                cost = profile.cost_class
                latency = profile.latency_class
            costs = dict(getattr(budget_constraints, "candidate_costs", None) or {})
            if selected_provider in costs:
                estimated = costs[selected_provider]
        max_affordable = getattr(budget_constraints, "max_affordable_cost", None)
        health_state = health_reason = None
        # Health gate applies to auto only; keep factor health fields empty for
        # explicit/both so decision equality stays stable across executions.
        if mode == MODE_AUTO and selected_provider:
            snap = self._health_snapshot(selected_provider)
            if snap is not None:
                health_state = snap.state
                health_reason = snap.reason_code
        return build_factor_snapshot(
            mode=mode,
            category=category,
            requirements=requirements,
            routing_policy=self.registry.auto_routing_policy,
            selected_provider=selected_provider,
            selected_model=selected_model,
            quality_class=quality,
            cost_class=cost,
            latency_class=latency,
            estimated_cost=estimated,
            max_affordable_cost=max_affordable,
            health_state=health_state,
            health_reason=health_reason,
            extra={"role_id": role_id},
        )

    def _audit_rows_for_pool(
        self,
        *,
        pool: tuple[str, ...],
        requirements,
        budget_constraints,
        selected: tuple[str, ...],
    ) -> tuple[tuple[RoutingCandidateAudit, ...], tuple[RoutingCandidateAudit, ...]]:
        """Classify providers that entered a category/general/priority pool."""

        considered: list[RoutingCandidateAudit] = []
        selected_set = set(selected)
        capable = self._filter_by_requirements(pool, requirements)
        capable_set = set(capable)
        eligible = self._apply_budget_constraints(capable, budget_constraints)
        eligible_set = set(eligible)

        for provider_id in pool:
            model_id = self._model_id(provider_id)
            if provider_id not in capable_set:
                row = RoutingCandidateAudit(
                    provider_id,
                    model_id,
                    eligible=False,
                    rejection_reason=REJECT_CAPABILITY_MISMATCH,
                )
            elif provider_id not in eligible_set:
                row = RoutingCandidateAudit(
                    provider_id,
                    model_id,
                    eligible=False,
                    rejection_reason=self._budget_reject_reason(
                        provider_id, budget_constraints
                    ),
                )
            else:
                row = RoutingCandidateAudit(
                    provider_id,
                    model_id,
                    eligible=True,
                    rejection_reason=None,
                )
            considered.append(row)

        # Ensure selected appear eligible even if pool order differs.
        for provider_id in selected:
            if not any(c.provider_id == provider_id and c.eligible for c in considered):
                considered.append(
                    RoutingCandidateAudit(
                        provider_id,
                        self._model_id(provider_id),
                        eligible=True,
                    )
                )

        rejected = tuple(c for c in considered if not c.eligible)
        # Prefer listing selected eligible first for stable audit order.
        eligible_rows = tuple(
            c for c in considered if c.eligible and c.provider_id in selected_set
        )
        other_eligible = tuple(
            c for c in considered if c.eligible and c.provider_id not in selected_set
        )
        return eligible_rows + other_eligible + rejected, rejected

    def _audit_registry_availability(
        self,
        *,
        include_available_as_eligible: bool,
        selected: tuple[str, ...] = (),
        extra_rejected: tuple[RoutingCandidateAudit, ...] = (),
    ) -> tuple[tuple[RoutingCandidateAudit, ...], tuple[RoutingCandidateAudit, ...]]:
        selected_set = set(selected)
        considered: list[RoutingCandidateAudit] = []
        for provider_id in PROVIDER_IDS:
            if provider_id not in self.registry._records:
                continue
            model_id = self._model_id(provider_id)
            if not self.registry.is_available(provider_id):
                considered.append(
                    RoutingCandidateAudit(
                        provider_id,
                        model_id,
                        eligible=False,
                        rejection_reason=REJECT_UNAVAILABLE,
                    )
                )
            elif include_available_as_eligible or provider_id in selected_set:
                considered.append(
                    RoutingCandidateAudit(
                        provider_id,
                        model_id,
                        eligible=True,
                    )
                )
        for row in extra_rejected:
            if not any(c.provider_id == row.provider_id for c in considered):
                considered.append(row)
        rejected = tuple(c for c in considered if not c.eligible)
        return tuple(considered), rejected

    def _decision(
        self,
        role_id: str,
        provider_ids: tuple[str, ...],
        reason: str,
        *,
        candidates_considered: tuple[RoutingCandidateAudit, ...] = (),
        rejected_candidates: tuple[RoutingCandidateAudit, ...] = (),
        factor_snapshot: RoutingFactorSnapshot | None = None,
    ) -> RoutingDecision:
        models = {
            provider_id: self.registry.model(provider_id)
            for provider_id in provider_ids
        }
        snapshot = factor_snapshot or EMPTY_FACTOR_SNAPSHOT
        decision = RoutingDecision(
            role_id=role_id,
            provider_ids=provider_ids,
            models=models,
            reason=reason,
            candidates_considered=candidates_considered,
            rejected_candidates=rejected_candidates,
            factor_snapshot=snapshot,
        )
        if self.observability is not None:
            from observability.helpers import safe_emit

            provider = provider_ids[0] if provider_ids else ""
            model = models.get(provider, "") if provider else ""
            metadata = routing_decision_audit_metadata(
                reason=reason,
                provider_ids=tuple(provider_ids),
                routing_policy_version=decision.routing_policy_version,
                candidates_considered=decision.candidates_considered,
                rejected_candidates=decision.rejected_candidates,
                factor_snapshot=decision.factor_snapshot,
            )
            safe_emit(
                self.observability,
                "provider.selected",
                context=self.observability.create_context(),
                component="provider",
                provider=provider,
                model=str(model or ""),
                status="selected",
                metadata=metadata,
            )
        return decision

    def decide(
        self,
        mode: str,
        role_id: str,
        category: str | None = None,
        budget_constraints=None,
        requirements=None,
    ) -> RoutingDecision:
        if mode == MODE_BOTH or mode == "both":
            # Limitation (documented): both keeps availability fan-out only.
            # TaskRequirements and routing-time budget filters are not applied
            # here to avoid silently dropping providers from an explicit
            # multi-provider request. Execution-time BudgetGuard still runs.
            provider_ids = self.registry.available_provider_ids()
            considered, rejected = self._audit_registry_availability(
                include_available_as_eligible=True,
                selected=provider_ids,
            )
            snapshot = self._factor_for(
                mode="both",
                role_id=role_id,
                category=category,
                requirements=requirements,
                budget_constraints=budget_constraints,
                selected=provider_ids,
            )
            return self._decision(
                role_id,
                provider_ids,
                REASON_ALL_AVAILABLE_PROVIDERS,
                candidates_considered=considered,
                rejected_candidates=rejected,
                factor_snapshot=snapshot,
            )

        if mode == MODE_AUTO:
            requested_category = category or routing_category_for_role(role_id)
            return self._decide_auto(
                role_id,
                requested_category,
                budget_constraints=budget_constraints,
                requirements=requirements,
            )

        return self._decide_explicit(
            mode=mode,
            role_id=role_id,
            category=category or routing_category_for_role(role_id),
            budget_constraints=budget_constraints,
            requirements=requirements,
        )

    def _budget_active(self, budget_constraints) -> bool:
        if budget_constraints is None:
            return False
        if getattr(budget_constraints, "excluded_providers", ()):
            return True
        if getattr(budget_constraints, "preferred_cheaper", ()):
            return True
        if getattr(budget_constraints, "max_affordable_cost", None) is not None:
            return True
        if getattr(budget_constraints, "candidate_costs", None):
            return True
        if getattr(budget_constraints, "unknown_cost_policy", None) is not None:
            return True
        return False

    def _apply_budget_constraints(
        self, provider_ids: tuple[str, ...], budget_constraints
    ) -> tuple[str, ...]:
        if budget_constraints is None:
            return tuple(provider_ids)
        excluded = set(getattr(budget_constraints, "excluded_providers", ()) or ())
        filtered = [p for p in provider_ids if p not in excluded]

        costs = dict(getattr(budget_constraints, "candidate_costs", None) or {})
        max_cost = getattr(budget_constraints, "max_affordable_cost", None)
        if costs or max_cost is not None:
            kept = []
            for provider_id in filtered:
                if provider_id in costs:
                    cost = costs[provider_id]
                    if cost is None:
                        # Unknown price is never treated as zero; eligibility was
                        # already decided when building constraints (allow/deny).
                        kept.append(provider_id)
                        continue
                    if max_cost is not None and cost > max_cost:
                        continue
                    kept.append(provider_id)
                else:
                    kept.append(provider_id)
            filtered = kept

        preferred = getattr(budget_constraints, "preferred_cheaper", ()) or ()
        if preferred:
            preferred_ids = tuple(p for p, _m in preferred if p in filtered)
            if preferred_ids:
                return preferred_ids
        return tuple(filtered)

    def _filter_by_requirements(
        self, provider_ids: tuple[str, ...], requirements
    ) -> tuple[str, ...]:
        if requirements is None:
            return tuple(provider_ids)
        if (
            not getattr(requirements, "required_capabilities", ())
            and getattr(requirements, "context_requirement", None) != "long"
        ):
            return tuple(provider_ids)
        matched = []
        for provider_id in provider_ids:
            profile = self.registry.profile(provider_id)
            if profile_satisfies_requirements(profile, requirements):
                matched.append(provider_id)
        return tuple(matched)

    def _raise_budget_denied(
        self,
        *,
        provider: str | None = None,
        category: str | None = None,
        reason: str = "budget_no_affordable_capable_route",
        candidates_considered: tuple[RoutingCandidateAudit, ...] = (),
        rejected_candidates: tuple[RoutingCandidateAudit, ...] = (),
        factor_snapshot: RoutingFactorSnapshot | None = None,
    ):
        raise BudgetRoutingDeniedError(
            reason,
            provider=provider,
            category=category,
            candidates_considered=candidates_considered,
            rejected_candidates=rejected_candidates,
            factor_snapshot=factor_snapshot,
        )

    def _decide_explicit(
        self,
        *,
        mode: str,
        role_id: str,
        category: str,
        budget_constraints=None,
        requirements=None,
    ) -> RoutingDecision:
        provider_ids = (mode,)
        model_id = self._model_id(mode)
        snapshot = self._factor_for(
            mode=mode,
            role_id=role_id,
            category=category,
            requirements=requirements,
            budget_constraints=budget_constraints,
            selected=provider_ids,
        )

        if requirements is not None and self.registry.is_available(mode):
            profile = self.registry.profile(mode)
            missing = missing_capabilities(profile, requirements)
            if missing:
                rejected = (
                    RoutingCandidateAudit(
                        mode,
                        model_id,
                        eligible=False,
                        rejection_reason=REJECT_CAPABILITY_MISMATCH,
                    ),
                )
                raise ProviderCapabilityMismatchError(
                    mode,
                    missing_capabilities=missing,
                    category=category,
                    candidates_considered=rejected,
                    rejected_candidates=rejected,
                    factor_snapshot=snapshot,
                )

        if self._budget_active(budget_constraints):
            eligible = self._apply_budget_constraints(provider_ids, budget_constraints)
            if not eligible:
                # Explicit choice is authoritative — never silent re-route.
                reject_reason = self._budget_reject_reason(mode, budget_constraints)
                rejected = (
                    RoutingCandidateAudit(
                        mode,
                        model_id,
                        eligible=False,
                        rejection_reason=reject_reason,
                    ),
                )
                raise BudgetRoutingDeniedError(
                    getattr(budget_constraints, "reason_code", None)
                    or "budget_hard_limit_exceeded",
                    provider=mode,
                    category=category,
                    candidates_considered=rejected,
                    rejected_candidates=rejected,
                    factor_snapshot=snapshot,
                )

        if not self.registry.is_available(mode):
            considered = (
                RoutingCandidateAudit(
                    mode,
                    model_id,
                    eligible=False,
                    rejection_reason=REJECT_UNAVAILABLE,
                ),
            )
            # Selection unchanged: still return explicit provider id for RouterV2.
            return self._decision(
                role_id,
                provider_ids,
                REASON_EXPLICIT_PROVIDER,
                candidates_considered=considered,
                rejected_candidates=considered,
                factor_snapshot=snapshot,
            )

        considered = (
            RoutingCandidateAudit(mode, model_id, eligible=True),
        )
        return self._decision(
            role_id,
            provider_ids,
            REASON_EXPLICIT_PROVIDER,
            candidates_considered=considered,
            rejected_candidates=(),
            factor_snapshot=snapshot,
        )

    def _select_from_capable(
        self,
        *,
        role_id: str,
        category: str,
        capable: tuple[str, ...],
        budget_constraints,
        success_reason: str,
        requirements=None,
        category_pool: tuple[str, ...] | None = None,
    ) -> RoutingDecision:
        eligible = self._apply_budget_constraints(capable, budget_constraints)
        pool = category_pool if category_pool is not None else capable
        if eligible:
            selected = self._rank_providers(eligible)
            reason = success_reason
            if self._budget_active(budget_constraints) and set(eligible) != set(capable):
                reason = REASON_AUTO_BUDGET_MATCH
            selected_ids = (selected,)
            # Availability + health + unsupported category for providers outside pool.
            avail_rows: list[RoutingCandidateAudit] = []
            pool_set = set(pool)
            health_blocked = self._health_blocked_set(self.registry.active_provider_ids())
            for provider_id in PROVIDER_IDS:
                if provider_id not in self.registry._records:
                    continue
                if not self.registry.is_available(provider_id):
                    avail_rows.append(
                        RoutingCandidateAudit(
                            provider_id,
                            self._model_id(provider_id),
                            eligible=False,
                            rejection_reason=REJECT_UNAVAILABLE,
                        )
                    )
                elif provider_id in health_blocked:
                    avail_rows.append(
                        RoutingCandidateAudit(
                            provider_id,
                            self._model_id(provider_id),
                            eligible=False,
                            rejection_reason=REJECT_HEALTH_COOLDOWN,
                        )
                    )
                elif provider_id not in pool_set:
                    avail_rows.append(
                        RoutingCandidateAudit(
                            provider_id,
                            self._model_id(provider_id),
                            eligible=False,
                            rejection_reason=REJECT_UNSUPPORTED_CATEGORY,
                        )
                    )
            pool_considered, pool_rejected = self._audit_rows_for_pool(
                pool=pool,
                requirements=requirements,
                budget_constraints=budget_constraints,
                selected=selected_ids,
            )
            considered = tuple(avail_rows) + pool_considered
            rejected = tuple(c for c in considered if not c.eligible)
            snapshot = self._factor_for(
                mode=MODE_AUTO,
                role_id=role_id,
                category=category,
                requirements=requirements,
                budget_constraints=budget_constraints,
                selected=selected_ids,
            )
            return self._decision(
                role_id,
                selected_ids,
                reason,
                candidates_considered=considered,
                rejected_candidates=rejected,
                factor_snapshot=snapshot,
            )
        if capable and self._budget_active(budget_constraints):
            pool_considered, pool_rejected = self._audit_rows_for_pool(
                pool=capable,
                requirements=requirements,
                budget_constraints=budget_constraints,
                selected=(),
            )
            snapshot = self._factor_for(
                mode=MODE_AUTO,
                role_id=role_id,
                category=category,
                requirements=requirements,
                budget_constraints=budget_constraints,
                selected=(),
            )
            self._raise_budget_denied(
                category=category,
                candidates_considered=pool_considered,
                rejected_candidates=pool_rejected,
                factor_snapshot=snapshot,
            )
        raise NoCapableProviderError(category, reason="category")

    def _decide_auto(
        self,
        role_id: str,
        category: str,
        budget_constraints=None,
        requirements=None,
    ) -> RoutingDecision:
        if not self.registry.available_provider_ids():
            considered, rejected = self._audit_registry_availability(
                include_available_as_eligible=False,
                selected=(),
            )
            snapshot = self._factor_for(
                mode=MODE_AUTO,
                role_id=role_id,
                category=category,
                requirements=requirements,
                budget_constraints=budget_constraints,
                selected=(),
            )
            return self._decision(
                role_id,
                (),
                REASON_AUTO_PROVIDER,
                candidates_considered=considered,
                rejected_candidates=rejected,
                factor_snapshot=snapshot,
            )

        # Order: static availability/state → dynamic health → category →
        # hard capabilities → budget → rank.
        active = self.registry.active_provider_ids()
        health_eligible = self._filter_by_health(active)
        capable = tuple(
            provider_id
            for provider_id in health_eligible
            if (
                self.registry.profile(provider_id) is not None
                and category in self.registry.profile(provider_id).task_categories
            )
        )
        category_matches = capable
        capable = self._filter_by_requirements(capable, requirements)
        if capable:
            return self._select_from_capable(
                role_id=role_id,
                category=category,
                capable=capable,
                budget_constraints=budget_constraints,
                success_reason=REASON_AUTO_CAPABILITY_MATCH,
                requirements=requirements,
                category_pool=category_matches,
            )

        requirements_blocked = bool(category_matches) and not capable
        fail_reason = "requirements" if requirements_blocked else "category"
        missing = (
            tuple(getattr(requirements, "required_capabilities", ()) or ())
            if fail_reason == "requirements"
            else ()
        )

        health_blocked = self._health_blocked_set(active)
        health_rows = tuple(
            RoutingCandidateAudit(
                provider_id,
                self._model_id(provider_id),
                eligible=False,
                rejection_reason=REJECT_HEALTH_COOLDOWN,
            )
            for provider_id in active
            if provider_id in health_blocked
        )

        failure_considered, failure_rejected = self._audit_rows_for_pool(
            pool=category_matches or (),
            requirements=requirements,
            budget_constraints=budget_constraints,
            selected=(),
        )
        if not category_matches:
            # Mark available non-supporters / health-blocked.
            rows = []
            for provider_id in active:
                if provider_id in health_blocked:
                    rows.append(
                        RoutingCandidateAudit(
                            provider_id,
                            self._model_id(provider_id),
                            eligible=False,
                            rejection_reason=REJECT_HEALTH_COOLDOWN,
                        )
                    )
                else:
                    rows.append(
                        RoutingCandidateAudit(
                            provider_id,
                            self._model_id(provider_id),
                            eligible=False,
                            rejection_reason=REJECT_UNSUPPORTED_CATEGORY,
                        )
                    )
            failure_considered = tuple(rows)
            failure_rejected = failure_considered
        else:
            failure_considered = health_rows + failure_considered
            failure_rejected = tuple(c for c in failure_considered if not c.eligible)
        failure_snapshot = self._factor_for(
            mode=MODE_AUTO,
            role_id=role_id,
            category=category,
            requirements=requirements,
            budget_constraints=budget_constraints,
            selected=(),
        )

        fallback = self.registry.auto_capability_fallback
        if fallback == FALLBACK_GENERAL:
            general_before = tuple(
                provider_id
                for provider_id in health_eligible
                if (
                    self.registry.profile(provider_id) is not None
                    and "general" in self.registry.profile(provider_id).task_categories
                )
            )
            general = self._filter_by_requirements(general_before, requirements)
            if general:
                return self._select_from_capable(
                    role_id=role_id,
                    category=category,
                    capable=general,
                    budget_constraints=budget_constraints,
                    success_reason=REASON_AUTO_GENERAL_FALLBACK,
                    requirements=requirements,
                    category_pool=general_before,
                )
            if general_before and not general:
                fail_reason = "requirements"
                missing = tuple(getattr(requirements, "required_capabilities", ()) or ())
                failure_considered, failure_rejected = self._audit_rows_for_pool(
                    pool=general_before,
                    requirements=requirements,
                    budget_constraints=budget_constraints,
                    selected=(),
                )
            raise NoCapableProviderError(
                category,
                reason=fail_reason,
                missing_capabilities=missing,
                candidates_considered=failure_considered,
                rejected_candidates=failure_rejected,
                factor_snapshot=failure_snapshot,
            )

        if fallback == FALLBACK_PRIORITY:
            order = tuple(
                p
                for p in self.registry.auto_provider_order
                if self.registry.is_available(p) and p in set(health_eligible)
            )
            order_before = order
            order = self._filter_by_requirements(order, requirements)
            if not order:
                if order_before and requirements is not None and (
                    getattr(requirements, "required_capabilities", ())
                    or getattr(requirements, "context_requirement", None) == "long"
                ):
                    considered, rejected = self._audit_rows_for_pool(
                        pool=order_before,
                        requirements=requirements,
                        budget_constraints=budget_constraints,
                        selected=(),
                    )
                    raise NoCapableProviderError(
                        category,
                        reason="requirements",
                        missing_capabilities=tuple(
                            getattr(requirements, "required_capabilities", ()) or ()
                        ),
                        candidates_considered=considered,
                        rejected_candidates=rejected,
                        factor_snapshot=failure_snapshot,
                    )
                considered, rejected = self._audit_registry_availability(
                    include_available_as_eligible=False,
                    selected=(),
                )
                return self._decision(
                    role_id,
                    (),
                    REASON_AUTO_PRIORITY_FALLBACK,
                    candidates_considered=considered,
                    rejected_candidates=rejected,
                    factor_snapshot=failure_snapshot,
                )
            eligible = self._apply_budget_constraints(order, budget_constraints)
            if not eligible:
                if self._budget_active(budget_constraints):
                    considered, rejected = self._audit_rows_for_pool(
                        pool=order,
                        requirements=requirements,
                        budget_constraints=budget_constraints,
                        selected=(),
                    )
                    self._raise_budget_denied(
                        category=category,
                        candidates_considered=considered,
                        rejected_candidates=rejected,
                        factor_snapshot=failure_snapshot,
                    )
                return self._decision(
                    role_id,
                    (),
                    REASON_AUTO_PRIORITY_FALLBACK,
                    factor_snapshot=failure_snapshot,
                )
            selected = eligible[0]
            reason = REASON_AUTO_PRIORITY_FALLBACK
            if self._budget_active(budget_constraints) and set(eligible) != set(order):
                reason = REASON_AUTO_BUDGET_MATCH
            selected_ids = (selected,)
            considered, rejected = self._audit_rows_for_pool(
                pool=order_before,
                requirements=requirements,
                budget_constraints=budget_constraints,
                selected=selected_ids,
            )
            snapshot = self._factor_for(
                mode=MODE_AUTO,
                role_id=role_id,
                category=category,
                requirements=requirements,
                budget_constraints=budget_constraints,
                selected=selected_ids,
            )
            return self._decision(
                role_id,
                selected_ids,
                reason,
                candidates_considered=considered,
                rejected_candidates=rejected,
                factor_snapshot=snapshot,
            )

        if fallback == FALLBACK_ERROR:
            raise NoCapableProviderError(
                category,
                reason=fail_reason,
                missing_capabilities=missing,
                candidates_considered=failure_considered,
                rejected_candidates=failure_rejected,
                factor_snapshot=failure_snapshot,
            )

        raise NoCapableProviderError(
            category,
            reason=fail_reason,
            missing_capabilities=missing,
            candidates_considered=failure_considered,
            rejected_candidates=failure_rejected,
            factor_snapshot=failure_snapshot,
        )

    def _rank_providers(self, candidates: tuple[str, ...]) -> str:
        policy = self.registry.auto_routing_policy
        order_index = {
            provider_id: index
            for index, provider_id in enumerate(self.registry.auto_provider_order)
        }

        def tie_break(provider_id: str) -> int:
            return order_index.get(provider_id, len(order_index))

        if policy == POLICY_PRIORITY:
            return min(candidates, key=tie_break)

        if policy == POLICY_QUALITY:
            return min(
                candidates,
                key=lambda provider_id: (
                    -QUALITY_RANK[self.registry.profile(provider_id).quality_class],
                    tie_break(provider_id),
                ),
            )

        if policy == POLICY_COST:
            return min(
                candidates,
                key=lambda provider_id: (
                    -COST_RANK[self.registry.profile(provider_id).cost_class],
                    tie_break(provider_id),
                ),
            )

        if policy == POLICY_LATENCY:
            return min(
                candidates,
                key=lambda provider_id: (
                    -LATENCY_RANK[self.registry.profile(provider_id).latency_class],
                    tie_break(provider_id),
                ),
            )

        if policy == POLICY_BALANCED:
            return min(
                candidates,
                key=lambda provider_id: (
                    -balanced_score(self.registry.profile(provider_id)),
                    tie_break(provider_id),
                ),
            )

        return min(candidates, key=tie_break)
