"""Production activation runtime accessor."""

from __future__ import annotations

from production_activation.service import ProductionActivationService

_runtime: ProductionActivationService | None = None


def get_production_activation_runtime() -> ProductionActivationService:
    global _runtime
    if _runtime is None:
        _runtime = ProductionActivationService()
    return _runtime


def configure_production_activation_runtime(service: ProductionActivationService) -> ProductionActivationService:
    global _runtime
    _runtime = service
    return _runtime
