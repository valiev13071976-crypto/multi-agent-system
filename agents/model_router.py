from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from agents.provider_registry import PROVIDER_IDS, ProviderRegistry


REASON_EXPLICIT_PROVIDER = "explicit_provider"
REASON_ALL_AVAILABLE_PROVIDERS = "all_available_providers"
REASON_AUTO_PROVIDER = "auto_provider"

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

        if mode == MODE_AUTO:
            selected = None
            for provider_id in self.registry.auto_provider_order:
                if self.registry.is_available(provider_id):
                    selected = provider_id
                    break
            if selected is None:
                return RoutingDecision(
                    role_id=role_id,
                    provider_ids=(),
                    models={},
                    reason=REASON_AUTO_PROVIDER,
                )
            return RoutingDecision(
                role_id=role_id,
                provider_ids=(selected,),
                models={selected: self.registry.model(selected)},
                reason=REASON_AUTO_PROVIDER,
            )

        provider_ids = (mode,)
        models = {mode: self.registry.model(mode)}
        return RoutingDecision(
            role_id=role_id,
            provider_ids=provider_ids,
            models=models,
            reason=REASON_EXPLICIT_PROVIDER,
        )
