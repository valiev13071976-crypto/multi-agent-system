"""Production integration runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass

from integrations.production.factory import ProductionIntegrationBundle, build_production_integrations


@dataclass
class ProductionIntegrationRuntime:
    bundle: ProductionIntegrationBundle
    enabled: bool = True

    def health(self) -> dict:
        return {
            "production_integrations_status": "healthy" if self.enabled else "disabled",
            "providers": len(self.bundle.registry.providers),
            "credential_entries": len(self.bundle.credential_inventory),
        }

    def provider_matrix(self) -> list[dict]:
        return self.bundle.registry.list_metadata()

    def close(self) -> None:
        self.bundle.close()


def build_production_integration_runtime(
    *,
    env: dict | None = None,
    integration_service=None,
    health_tracker=None,
) -> ProductionIntegrationRuntime:
    source = env if env is not None else dict(os.environ)
    enabled = str(source.get("PRODUCTION_INTEGRATIONS_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        bundle = build_production_integrations(env=source, integration_service=integration_service, health_tracker=health_tracker)
        return ProductionIntegrationRuntime(bundle=bundle, enabled=False)
    bundle = build_production_integrations(env=source, integration_service=integration_service, health_tracker=health_tracker)
    return ProductionIntegrationRuntime(bundle=bundle, enabled=True)
