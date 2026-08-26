from dataclasses import dataclass
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


REASON_EXPLICIT_PROVIDER = "explicit_provider"
REASON_ALL_AVAILABLE_PROVIDERS = "all_available_providers"
REASON_AUTO_PROVIDER = "auto_provider"
REASON_AUTO_CAPABILITY_MATCH = "auto_capability_match"
REASON_AUTO_GENERAL_FALLBACK = "auto_general_fallback"
REASON_AUTO_PRIORITY_FALLBACK = "auto_priority_fallback"
REASON_AUTO_REQUIREMENTS_MATCH = "auto_requirements_match"
REASON_EXPLICIT_CAPABILITY_MISMATCH = "explicit_capability_mismatch"

EXPLICIT_MODES = frozenset(PROVIDER_IDS)
MODE_AUTO = "auto"


@dataclass(frozen=True)
class RoutingDecision:
    role_id: str
    provider_ids: tuple[str, ...]
    models: Mapping[str, str]
    reason: str
    routing_policy_version: str = ROUTING_POLICY_VERSION

    def __post_init__(self):
        object.__setattr__(self, "provider_ids", tuple(self.provider_ids))
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))
        object.__setattr__(
            self,
            "routing_policy_version",
            self.routing_policy_version or ROUTING_POLICY_VERSION,
        )


class NoCapableProviderError(Exception):
    def __init__(
        self,
        category: str,
        *,
        reason: str = "category",
        missing_capabilities: tuple[str, ...] = (),
    ):
        self.category = category
        self.reason = reason
        self.missing_capabilities = tuple(missing_capabilities or ())
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
    ):
        self.provider = provider
        self.missing_capabilities = tuple(missing_capabilities or ())
        self.category = category
        super().__init__(
            f"Provider {provider!r} does not satisfy required capabilities."
        )


class ModelRouter:
    """
    Formalizes mode → provider selection.
    Does not inspect the user prompt or classify tasks.

    ``mode=both`` intentionally does not apply TaskRequirements capability
    filtering (preserves multi-provider fan-out semantics). Capability
    filtering applies to ``auto`` and validates explicit single-provider mode.
    """

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry
        self.observability = None

    def _decision(self, role_id: str, provider_ids: tuple[str, ...], reason: str) -> RoutingDecision:
        models = {
            provider_id: self.registry.model(provider_id)
            for provider_id in provider_ids
        }
        decision = RoutingDecision(
            role_id=role_id,
            provider_ids=provider_ids,
            models=models,
            reason=reason,
        )
        if self.observability is not None:
            from observability.helpers import safe_emit

            provider = provider_ids[0] if provider_ids else ""
            model = models.get(provider, "") if provider else ""
            safe_emit(
                self.observability,
                "provider.selected",
                context=self.observability.create_context(),
                component="provider",
                provider=provider,
                model=str(model or ""),
                status="selected",
                metadata={
                    "route_reason": reason,
                    "capability_match": reason,
                    "provider_count": len(provider_ids),
                },
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
        if mode == "both":
            # Limitation (documented): both keeps availability fan-out only.
            # TaskRequirements capability filtering is not applied here to avoid
            # silently dropping providers from an explicit multi-provider request.
            provider_ids = self.registry.available_provider_ids()
            provider_ids = self._apply_budget_constraints(provider_ids, budget_constraints)
            return self._decision(
                role_id,
                provider_ids,
                REASON_ALL_AVAILABLE_PROVIDERS,
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

    def _apply_budget_constraints(
        self, provider_ids: tuple[str, ...], budget_constraints
    ) -> tuple[str, ...]:
        if budget_constraints is None:
            return tuple(provider_ids)
        excluded = set(getattr(budget_constraints, "excluded_providers", ()) or ())
        filtered = tuple(p for p in provider_ids if p not in excluded)
        preferred = getattr(budget_constraints, "preferred_cheaper", ()) or ()
        if preferred:
            preferred_ids = tuple(p for p, _m in preferred if p in filtered)
            if preferred_ids:
                return preferred_ids
        return filtered

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
        provider_ids = self._apply_budget_constraints(provider_ids, budget_constraints)
        if not provider_ids and budget_constraints is not None:
            return self._decision(role_id, (), REASON_EXPLICIT_PROVIDER)

        if provider_ids and requirements is not None:
            provider_id = provider_ids[0]
            # Unavailable providers keep provider_not_configured semantics in RouterV2.
            if self.registry.is_available(provider_id):
                profile = self.registry.profile(provider_id)
                missing = missing_capabilities(profile, requirements)
                if missing:
                    raise ProviderCapabilityMismatchError(
                        provider_id,
                        missing_capabilities=missing,
                        category=category,
                    )

        return self._decision(role_id, provider_ids, REASON_EXPLICIT_PROVIDER)

    def _decide_auto(
        self,
        role_id: str,
        category: str,
        budget_constraints=None,
        requirements=None,
    ) -> RoutingDecision:
        if not self.registry.available_provider_ids():
            return self._decision(role_id, (), REASON_AUTO_PROVIDER)

        capable = self.registry.providers_supporting(category)
        capable = self._apply_budget_constraints(capable, budget_constraints)
        category_matches = capable
        capable = self._filter_by_requirements(capable, requirements)
        if capable:
            selected = self._rank_providers(capable)
            return self._decision(
                role_id,
                (selected,),
                REASON_AUTO_CAPABILITY_MATCH,
            )

        requirements_blocked = bool(category_matches) and not capable
        fail_reason = "requirements" if requirements_blocked else "category"
        missing = (
            tuple(getattr(requirements, "required_capabilities", ()) or ())
            if fail_reason == "requirements"
            else ()
        )

        fallback = self.registry.auto_capability_fallback
        if fallback == FALLBACK_GENERAL:
            general = self.registry.providers_supporting("general")
            general = self._apply_budget_constraints(general, budget_constraints)
            general_before = general
            general = self._filter_by_requirements(general, requirements)
            if general:
                selected = self._rank_providers(general)
                return self._decision(
                    role_id,
                    (selected,),
                    REASON_AUTO_GENERAL_FALLBACK,
                )
            if general_before and not general:
                fail_reason = "requirements"
                missing = tuple(getattr(requirements, "required_capabilities", ()) or ())
            raise NoCapableProviderError(
                category,
                reason=fail_reason,
                missing_capabilities=missing,
            )

        if fallback == FALLBACK_PRIORITY:
            order = self._apply_budget_constraints(
                tuple(
                    p
                    for p in self.registry.auto_provider_order
                    if self.registry.is_available(p)
                ),
                budget_constraints,
            )
            order_before = order
            order = self._filter_by_requirements(order, requirements)
            if not order:
                if order_before and requirements is not None and (
                    getattr(requirements, "required_capabilities", ())
                    or getattr(requirements, "context_requirement", None) == "long"
                ):
                    raise NoCapableProviderError(
                        category,
                        reason="requirements",
                        missing_capabilities=tuple(
                            getattr(requirements, "required_capabilities", ()) or ()
                        ),
                    )
                return self._decision(role_id, (), REASON_AUTO_PRIORITY_FALLBACK)
            selected = order[0]
            return self._decision(
                role_id,
                (selected,),
                REASON_AUTO_PRIORITY_FALLBACK,
            )

        if fallback == FALLBACK_ERROR:
            raise NoCapableProviderError(
                category,
                reason=fail_reason,
                missing_capabilities=missing,
            )

        raise NoCapableProviderError(
            category,
            reason=fail_reason,
            missing_capabilities=missing,
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
