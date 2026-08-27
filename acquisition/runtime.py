"""Composition bootstrap for Data Acquisition & Parsing Platform."""

from __future__ import annotations

import os

from acquisition.parsers import build_default_parser_registry
from acquisition.registry import SourceRegistry
from acquisition.schedule import AcquisitionScheduler
from acquisition.service import AcquisitionService
from acquisition.store import AcquisitionStore, InMemoryAcquisitionStore
from workflow.schedule import WorkflowScheduler


def acquisition_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("ACQUISITION_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def acquisition_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    return {
        "enabled": acquisition_enabled(source),
        "backend": str(source.get("ACQUISITION_BACKEND", "memory") or "memory").strip().lower(),
        "db_path": str(source.get("ACQUISITION_DB_PATH") or "").strip() or None,
        "use_shared_db": str(source.get("ACQUISITION_USE_SHARED_DB", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
    }


def build_acquisition_store(
    *,
    env: dict | None = None,
    shared_connection=None,
    require_durable: bool = False,
) -> AcquisitionStore:
    cfg = acquisition_config(env)
    backend = cfg["backend"]
    if backend in {"sqlite", "durable"} or cfg["db_path"]:
        from acquisition.sqlite_store import (
            AcquisitionStoreUnavailableError,
            SqliteAcquisitionStore,
        )

        path = cfg["db_path"]
        if shared_connection is not None and not path:
            return SqliteAcquisitionStore(
                shared_connection=shared_connection, owns_connection=False
            )
        if path:
            return SqliteAcquisitionStore(db_path=path, owns_connection=True)
        if require_durable:
            raise AcquisitionStoreUnavailableError()
        if shared_connection is not None:
            return SqliteAcquisitionStore(
                shared_connection=shared_connection, owns_connection=False
            )
        raise AcquisitionStoreUnavailableError()
    if require_durable:
        from acquisition.sqlite_store import AcquisitionStoreUnavailableError

        raise AcquisitionStoreUnavailableError()
    return InMemoryAcquisitionStore()


def _hydrate_source_registry(registry: SourceRegistry, store: AcquisitionStore) -> None:
    if not hasattr(store, "_connect"):
        return
    try:
        with store._lock:  # noqa: SLF001
            conn = store._connect()  # noqa: SLF001
            rows = conn.execute(
                "SELECT DISTINCT tenant_id FROM acquisition_sources"
            ).fetchall()
        for row in rows:
            tid = row["tenant_id"] if not isinstance(row, tuple) else row[0]
            for desc in store.list_sources(tenant_id=str(tid)):
                try:
                    registry.register(desc)
                except Exception:
                    pass
    except Exception:
        pass


class AcquisitionRuntime:
    def __init__(
        self,
        *,
        service: AcquisitionService,
        store: AcquisitionStore,
        enabled: bool = True,
    ):
        self.service = service
        self.store = store
        self.enabled = bool(enabled)

    def health(self) -> dict:
        ready = True
        backend = getattr(self.store, "persistence_backend", "memory")
        if hasattr(self.store, "available"):
            ready = bool(self.store.available)
        return {
            "acquisition_status": "healthy" if self.enabled and ready else "degraded",
            "enabled": self.enabled,
            "persistence_ready": ready,
            "persistence_backend": backend,
            "connection_mode": getattr(self.store, "connection_mode", "memory"),
        }

    def close(self) -> None:
        if hasattr(self.store, "close"):
            try:
                self.store.close()
            except Exception:
                pass


def build_acquisition_runtime(
    *,
    tool_gateway=None,
    workflow_scheduler: WorkflowScheduler | None = None,
    freeze_sources: bool = False,
    store: AcquisitionStore | None = None,
) -> AcquisitionService:
    """Unit-test / lightweight builder — returns AcquisitionService."""
    sources = SourceRegistry()
    resolved = store or InMemoryAcquisitionStore()
    _hydrate_source_registry(sources, resolved)
    service = AcquisitionService(
        source_registry=sources,
        store=resolved,
        parser_registry=build_default_parser_registry(),
        tool_gateway=tool_gateway,
        scheduler=AcquisitionScheduler(workflow_scheduler or WorkflowScheduler()),
    )
    if freeze_sources:
        sources.freeze()
    return service


def build_acquisition_runtime_bundle(
    *,
    tool_gateway=None,
    workflow_scheduler: WorkflowScheduler | None = None,
    env: dict | None = None,
    shared_connection=None,
    freeze_sources: bool = False,
) -> AcquisitionRuntime:
    """Production composition — durable store when configured / shared DB available."""
    cfg = acquisition_config(env)
    if not cfg["enabled"]:
        store = InMemoryAcquisitionStore()
        service = AcquisitionService(
            source_registry=SourceRegistry(),
            store=store,
            parser_registry=build_default_parser_registry(),
            tool_gateway=tool_gateway,
            scheduler=AcquisitionScheduler(workflow_scheduler or WorkflowScheduler()),
        )
        return AcquisitionRuntime(service=service, store=store, enabled=False)

    store: AcquisitionStore
    if cfg["backend"] in {"sqlite", "durable"} or cfg["db_path"]:
        store = build_acquisition_store(
            env=env, shared_connection=shared_connection, require_durable=False
        )
    elif shared_connection is not None and cfg["use_shared_db"]:
        from acquisition.sqlite_store import SqliteAcquisitionStore

        try:
            store = SqliteAcquisitionStore(
                shared_connection=shared_connection, owns_connection=False
            )
        except Exception:
            store = InMemoryAcquisitionStore()
    else:
        store = InMemoryAcquisitionStore()

    sources = SourceRegistry()
    _hydrate_source_registry(sources, store)
    service = AcquisitionService(
        source_registry=sources,
        store=store,
        parser_registry=build_default_parser_registry(),
        tool_gateway=tool_gateway,
        scheduler=AcquisitionScheduler(workflow_scheduler or WorkflowScheduler()),
    )
    if freeze_sources:
        sources.freeze()
    return AcquisitionRuntime(service=service, store=store, enabled=True)
