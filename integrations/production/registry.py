"""Production provider registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from integrations.production.metadata import ProviderMetadata


@dataclass
class ProductionProviderRegistry:
    providers: dict[str, ProviderMetadata] = field(default_factory=dict)
    adapters: dict[str, Any] = field(default_factory=dict)

    def register(self, meta: ProviderMetadata, adapter: Any | None = None) -> None:
        self.providers[meta.provider_id] = meta
        if adapter is not None:
            self.adapters[meta.provider_id] = adapter

    def get(self, provider_id: str) -> ProviderMetadata | None:
        return self.providers.get(provider_id)

    def list_metadata(self) -> list[dict]:
        return [m.as_dict() for m in self.providers.values()]

    def update_health(self, provider_id: str, *, health_state: str, circuit_state: str = "", failure_category: str = "") -> None:
        meta = self.providers.get(provider_id)
        if meta is None:
            return
        meta.health_state = health_state
        if circuit_state:
            meta.circuit_state = circuit_state
        if failure_category:
            meta.last_failure_category = failure_category
