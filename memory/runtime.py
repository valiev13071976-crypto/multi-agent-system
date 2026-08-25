"""MemoryRuntime composition — no hidden globals."""

from __future__ import annotations

import os
from pathlib import Path

from memory.access import MemoryAccessPolicy
from memory.context_builder import KnowledgeContextBuilder
from memory.embeddings import NullEmbeddingProvider, NullVectorIndex
from memory.retention import MemoryRetentionPolicy
from memory.retrieval import MemoryRetriever
from memory.service import MemoryService
from memory.sqlite_store import SqliteMemoryStore
from memory.store import InMemoryMemoryStore, MemoryPersistenceUnavailableError
from memory.write_policy import MemoryWritePolicy
from security.encryption import EncryptionService


def memory_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("MEMORY_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def memory_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    return {
        "enabled": memory_enabled(source),
        "backend": str(source.get("MEMORY_BACKEND", "memory") or "memory").strip().lower(),
        "db_path": str(source.get("MEMORY_DB_PATH") or "").strip() or None,
        "max_record_bytes": int(source.get("MEMORY_MAX_RECORD_BYTES", "32768") or 32768),
        "episodic_ttl_days": int(source.get("MEMORY_DEFAULT_EPISODIC_TTL_DAYS", "90") or 90),
        "working_ttl_hours": int(source.get("MEMORY_WORKING_REFERENCE_TTL_HOURS", "24") or 24),
    }


def build_memory_store(
    *,
    env: dict | None = None,
    shared_connection=None,
    require_durable: bool = False,
):
    cfg = memory_config(env)
    backend = cfg["backend"]
    if backend in {"sqlite", "durable"} or cfg["db_path"]:
        path = cfg["db_path"]
        if shared_connection is not None and not path:
            return SqliteMemoryStore(shared_connection=shared_connection, owns_connection=False)
        if path:
            return SqliteMemoryStore(db_path=path, owns_connection=True)
        if require_durable:
            raise MemoryPersistenceUnavailableError("memory_persistence_unavailable")
        if shared_connection is not None:
            return SqliteMemoryStore(shared_connection=shared_connection, owns_connection=False)
        raise MemoryPersistenceUnavailableError("memory_db_path_required")
    if require_durable:
        raise MemoryPersistenceUnavailableError("memory_persistence_unavailable")
    return InMemoryMemoryStore()


class MemoryRuntime:
    def __init__(
        self,
        *,
        service: MemoryService,
        store,
        retriever: MemoryRetriever,
        access: MemoryAccessPolicy,
        retention: MemoryRetentionPolicy,
        embedding_provider=None,
        vector_index=None,
        fts_fallback: bool = False,
        enabled: bool = True,
    ):
        self.service = service
        self.store = store
        self.retriever = retriever
        self.access = access
        self.retention = retention
        self.embedding_provider = embedding_provider or NullEmbeddingProvider()
        self.vector_index = vector_index or NullVectorIndex()
        self.fts_fallback = bool(fts_fallback)
        self.enabled = bool(enabled)

    def health(self) -> dict:
        ready = bool(getattr(self.store, "available", True))
        mode = getattr(self.store, "connection_mode", "memory")
        status = "healthy"
        if self.fts_fallback or (
            hasattr(self.store, "fts_available") and not self.store.fts_available
            and mode != "memory"
        ):
            status = "degraded"
        if self.enabled and not ready:
            status = "blocked"
        if self.service.blocked_reason:
            status = "blocked"
        return {
            "memory_status": status,
            "persistence_backend": getattr(self.store, "persistence_backend", "memory"),
            "persistence_ready": ready,
            "connection_mode": mode,
            "fts_available": bool(getattr(self.store, "fts_available", False)),
            "enabled": self.enabled,
        }

    def close(self) -> None:
        if hasattr(self.store, "close"):
            try:
                self.store.close()
            except Exception:
                pass


def build_memory_runtime(
    *,
    env: dict | None = None,
    encryption: EncryptionService | None = None,
    observability=None,
    shared_connection=None,
) -> MemoryRuntime | None:
    cfg = memory_config(env)
    if not cfg["enabled"]:
        return None
    require_durable = cfg["backend"] in {"sqlite", "durable"}
    try:
        store = build_memory_store(
            env=env,
            shared_connection=shared_connection,
            require_durable=require_durable,
        )
    except MemoryPersistenceUnavailableError:
        if require_durable:
            store = InMemoryMemoryStore()
            store.available = False
            service = MemoryService(
                store,
                encryption=encryption,
                observability=observability,
                max_record_bytes=cfg["max_record_bytes"],
                enabled=True,
            )
            service.blocked_reason = "memory_persistence_unavailable"
            return MemoryRuntime(
                service=service,
                store=store,
                retriever=MemoryRetriever(encryption=encryption),
                access=MemoryAccessPolicy(),
                retention=MemoryRetentionPolicy(),
                enabled=True,
            )
        raise

    retention = MemoryRetentionPolicy(
        episodic_ttl_days=cfg["episodic_ttl_days"],
        working_reference_ttl_hours=cfg["working_ttl_hours"],
    )
    access = MemoryAccessPolicy()
    retriever = MemoryRetriever(encryption=encryption)
    service = MemoryService(
        store,
        access=access,
        retention=retention,
        retriever=retriever,
        write_policy=MemoryWritePolicy(),
        encryption=encryption,
        context_builder=KnowledgeContextBuilder(),
        observability=observability,
        max_record_bytes=cfg["max_record_bytes"],
        enabled=True,
    )
    fts_fallback = not bool(getattr(store, "fts_available", False)) and getattr(
        store, "connection_mode", ""
    ) != "memory"
    return MemoryRuntime(
        service=service,
        store=store,
        retriever=retriever,
        access=access,
        retention=retention,
        embedding_provider=NullEmbeddingProvider(),
        vector_index=NullVectorIndex(),
        fts_fallback=fts_fallback or retriever.fts_fallback_active,
        enabled=True,
    )
