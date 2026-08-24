from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from agents.provider_registry import PROVIDER_IDS, ProviderRegistry


REASON_EXPLICIT_PROVIDER = "explicit_provider"
REASON_ALL_AVAILABLE_PROVIDERS = "all_available_providers"

EXPLICIT_MODES = frozenset(PROVIDER_IDS)


@dataclass(frozen=True)
class RoutingDecision:
    role_id: str
    provider_ids: tuple[str, ...]
    models: Mapping[str, str]
    reason: str

    def __post_init__(self):
        object.__setattr__(self, "provider_ids", tuple(self.provider_ids))
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))


class ModelRouter:
    """
    Formalizes the existing mode → provider selection.
    Does not inspect the user prompt.
    """

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry

    def decide(self, mode: str, role_id: str) -> RoutingDecision:
        if mode == "both":
            provider_ids = self.registry.available_provider_ids()
            models = {
                provider_id: self.registry.model(provider_id)
                for provider_id in provider_ids
            }
            return RoutingDecision(
                role_id=role_id,
                provider_ids=provider_ids,
                models=models,
                reason=REASON_ALL_AVAILABLE_PROVIDERS,
            )

        provider_ids = (mode,)
        models = {mode: self.registry.model(mode)}
        return RoutingDecision(
            role_id=role_id,
            provider_ids=provider_ids,
            models=models,
            reason=REASON_EXPLICIT_PROVIDER,
        )
