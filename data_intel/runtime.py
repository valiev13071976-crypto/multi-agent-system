"""Data Intelligence runtime composition — production bootstrap."""

from __future__ import annotations

import os

from data_intel.large import LargeDatasetPolicy
from data_intel.service import DataIntelligenceService
from data_intel.store import InMemoryDatasetStore, SqliteDatasetStore
from data_intel.workflow_def import register_data_intel_workflows


def data_intel_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("DATA_INTEL_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def data_intel_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    return {
        "enabled": data_intel_enabled(source),
        "backend": str(source.get("DATA_INTEL_BACKEND", "sqlite") or "sqlite").strip().lower(),
        "db_path": str(source.get("DATA_INTEL_DB_PATH") or "").strip() or None,
        "use_shared_db": str(source.get("DATA_INTEL_USE_SHARED_DB", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
    }


def build_data_intel_store(
    *,
    env: dict | None = None,
    shared_connection=None,
    require_durable: bool = False,
):
    """Prefer SqliteDatasetStore. Use shared side-effect DB when available."""
    cfg = data_intel_config(env)
    backend = cfg["backend"]
    if shared_connection is not None and cfg["use_shared_db"]:
        return SqliteDatasetStore(shared_connection=shared_connection, owns_connection=False)
    if cfg["db_path"]:
        return SqliteDatasetStore(db_path=cfg["db_path"], owns_connection=True)
    if backend in {"sqlite", "durable"}:
        if shared_connection is not None:
            return SqliteDatasetStore(shared_connection=shared_connection, owns_connection=False)
        if require_durable:
            from data_intel.errors import DATASET_STORE_UNAVAILABLE, DataIntelError

            raise DataIntelError(DATASET_STORE_UNAVAILABLE)
        # Process-local sqlite (still SqliteDatasetStore, not InMemory)
        return SqliteDatasetStore(path=":memory:", owns_connection=True)
    if backend in {"memory", "in_memory"}:
        return InMemoryDatasetStore()
    if shared_connection is not None:
        return SqliteDatasetStore(shared_connection=shared_connection, owns_connection=False)
    return SqliteDatasetStore(path=":memory:", owns_connection=True)


class DataIntelligenceRuntime:
    def __init__(
        self,
        *,
        service: DataIntelligenceService,
        store,
        enabled: bool = True,
    ):
        self.service = service
        self.store = store
        self.enabled = bool(enabled)

    def health(self) -> dict:
        return {
            "data_intel_status": "healthy" if self.enabled else "disabled",
            "persistence_backend": getattr(self.store, "persistence_backend", "memory"),
            "connection_mode": getattr(self.store, "connection_mode", "memory"),
            "enabled": self.enabled,
        }


def build_data_intelligence_runtime(
    *,
    env: dict | None = None,
    document_service=None,
    workflow_runtime=None,
    acquisition_service=None,
    observability=None,
    shared_connection=None,
    store=None,
    large_policy: LargeDatasetPolicy | None = None,
) -> DataIntelligenceRuntime | None:
    """Compose DataIntelligenceService onto existing Documents/Workflow instances."""
    cfg = data_intel_config(env)
    if not cfg["enabled"]:
        return None
    ds_store = store or build_data_intel_store(env=env, shared_connection=shared_connection)
    service = DataIntelligenceService(
        ds_store,
        large_policy=large_policy or LargeDatasetPolicy(),
        workflow_runtime=workflow_runtime,
        document_service=document_service,
        observability=observability,
    )
    if acquisition_service is not None:
        service.acquisition_service = acquisition_service
    if workflow_runtime is not None:
        try:
            register_data_intel_workflows(
                workflow_runtime.definitions, workflow_runtime.platform
            )
        except Exception:
            pass
        engine = getattr(workflow_runtime, "platform", None)
        engine = getattr(engine, "workflow_engine", None) or getattr(
            workflow_runtime, "workflow_engine", None
        )
        if engine is not None:
            engine.data_intelligence = service
    return DataIntelligenceRuntime(service=service, store=ds_store, enabled=True)
