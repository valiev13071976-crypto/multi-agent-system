"""Production activation runtime accessor — durable store across CLI processes."""

from __future__ import annotations

from production_activation.paths import open_production_activation_store
from production_activation.service import ProductionActivationService

_runtime: ProductionActivationService | None = None


def get_production_activation_runtime(*, env: dict | None = None) -> ProductionActivationService:
    """Return process-local runtime backed by the canonical durable SQLite path.

    Each OS process constructs its own service instance, but all processes share
    the same on-disk database under PANDA_DATA_DIR / PRODUCTION_ACTIVATION_DB_PATH.
    Construction never activates GO LIVE.
    """
    global _runtime
    if _runtime is None:
        _runtime = ProductionActivationService(store=open_production_activation_store(env))
    return _runtime


def configure_production_activation_runtime(service: ProductionActivationService) -> ProductionActivationService:
    global _runtime
    _runtime = service
    return _runtime


def reset_production_activation_runtime() -> None:
    """Drop process-local singleton (does not delete durable DB; does not activate)."""
    global _runtime
    if _runtime is not None:
        try:
            _runtime.store.close()
        except Exception:
            pass
    _runtime = None
