from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

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

EXPLICIT_MODES = frozenset(PROVIDER_IDS)
MODE_AUTO = "auto"


@dataclass(frozen=True)
class RoutingDecision:
    role_id: str
    provider_ids: tuple[str, ...]
    models: Mapping[str, str]
    reason: str

    def __post_init__(self):
        object.__setattr__(self, "provider_ids", tuple(self.provider_ids))
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))


class NoCapableProviderError(Exception):
    def __init__(self, category: str):
        self.category = category
        super().__init__(
            f"No configured provider supports task category {category!r}."
        )


class ModelRouter:
    """
    Formalizes mode → provider selection.
    Does not inspect the user prompt or classify tasks.
    """

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry

    def _decision(self, role_id: str, provider_ids: tuple[str, ...], reason: str) -> RoutingDecision:
        models = {
            provider_id: self.registry.model(provider_id)
            for provider_id in provider_ids
        }
        return RoutingDecision(
            role_id=role_id,
            provider_ids=provider_ids,
            models=models,
            reason=reason,
        )

    def decide(
        self,
        mode: str,
        role_id: str,
        category: str | None = None,
    ) -> RoutingDecision:
        if mode == "both":
            provider_ids = self.registry.available_provider_ids()
            return self._decision(
                role_id,
                provider_ids,
                REASON_ALL_AVAILABLE_PROVIDERS,
            )

        if mode == MODE_AUTO:
            requested_category = category or routing_category_for_role(role_id)
            return self._decide_auto(role_id, requested_category)

        provider_ids = (mode,)
        return self._decision(role_id, provider_ids, REASON_EXPLICIT_PROVIDER)

    def _decide_auto(self, role_id: str, category: str) -> RoutingDecision:
        if not self.registry.available_provider_ids():
            return self._decision(role_id, (), REASON_AUTO_PROVIDER)

        capable = self.registry.providers_supporting(category)
        if capable:
            selected = self._rank_providers(capable)
            return self._decision(
                role_id,
                (selected,),
                REASON_AUTO_CAPABILITY_MATCH,
            )

        fallback = self.registry.auto_capability_fallback
        if fallback == FALLBACK_GENERAL:
            general = self.registry.providers_supporting("general")
            if general:
                selected = self._rank_providers(general)
                return self._decision(
                    role_id,
                    (selected,),
                    REASON_AUTO_GENERAL_FALLBACK,
                )
            raise NoCapableProviderError(category)

        if fallback == FALLBACK_PRIORITY:
            selected = None
            for provider_id in self.registry.auto_provider_order:
                if self.registry.is_available(provider_id):
                    selected = provider_id
                    break
            if selected is None:
                return self._decision(role_id, (), REASON_AUTO_PRIORITY_FALLBACK)
            return self._decision(
                role_id,
                (selected,),
                REASON_AUTO_PRIORITY_FALLBACK,
            )

        if fallback == FALLBACK_ERROR:
            raise NoCapableProviderError(category)

        raise NoCapableProviderError(category)

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
