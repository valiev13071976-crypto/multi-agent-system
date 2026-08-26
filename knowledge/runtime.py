"""KnowledgeRuntime composition — offline-first, no network on startup."""

from __future__ import annotations

import os

from knowledge.access import KnowledgeAccessPolicy
from knowledge.adapters import DocumentKnowledgeAdapter, MemoryKnowledgeAdapter
from knowledge.models import (
    DEFAULT_MAX_CONTEXT_BYTES,
    DEFAULT_MAX_ITEM_BYTES,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MAX_SOURCES,
    DEFAULT_TTL_SECONDS,
    SOURCE_DOCUMENT,
    SOURCE_MEMORY,
    TRUST_DOCUMENT,
    TRUST_VALIDATED_INTERNAL,
    FreshnessPolicy,
    KnowledgeSource,
)
from knowledge.rag_context import RAGContextBuilder
from knowledge.registry import KnowledgeSourceRegistry
from knowledge.service import KnowledgeService
from knowledge.validator import KnowledgeValidator
from knowledge.write_policy import KnowledgeWritePolicy
from memory.models import SCOPE_WORKSPACE, MemoryScope, utc_now


def knowledge_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("KNOWLEDGE_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def knowledge_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    return {
        "enabled": knowledge_enabled(source),
        "max_sources": int(source.get("KNOWLEDGE_MAX_SOURCES", str(DEFAULT_MAX_SOURCES)) or DEFAULT_MAX_SOURCES),
        "max_results": int(source.get("KNOWLEDGE_MAX_RESULTS", str(DEFAULT_MAX_RESULTS)) or DEFAULT_MAX_RESULTS),
        "max_item_bytes": int(
            source.get("KNOWLEDGE_MAX_ITEM_BYTES", str(DEFAULT_MAX_ITEM_BYTES)) or DEFAULT_MAX_ITEM_BYTES
        ),
        "max_context_bytes": int(
            source.get("KNOWLEDGE_MAX_CONTEXT_BYTES", str(DEFAULT_MAX_CONTEXT_BYTES))
            or DEFAULT_MAX_CONTEXT_BYTES
        ),
        "default_ttl_seconds": int(
            source.get("KNOWLEDGE_DEFAULT_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)) or DEFAULT_TTL_SECONDS
        ),
    }


class KnowledgeRuntime:
    def __init__(
        self,
        *,
        service: KnowledgeService,
        registry: KnowledgeSourceRegistry,
        rag_builder: RAGContextBuilder,
        validator: KnowledgeValidator,
        write_policy: KnowledgeWritePolicy,
        enabled: bool = True,
    ):
        self.service = service
        self.registry = registry
        self.rag_builder = rag_builder
        self.validator = validator
        self.write_policy = write_policy
        self.enabled = bool(enabled)

    def health(self) -> dict:
        status = "healthy"
        if self.enabled and self.service.blocked_reason:
            status = "blocked"
        elif self.service.memory_service is None and self.service.document_service is None:
            status = "degraded"
        return {
            "knowledge_status": status,
            "enabled": self.enabled,
            "source_count": len(self.registry.list_sources()),
            "frozen": self.registry.frozen,
            "persistence_ready": self.service.memory_service is not None,
        }

    def close(self) -> None:
        return None


def build_knowledge_runtime(
    *,
    env: dict | None = None,
    memory_service=None,
    document_service=None,
    tool_gateway=None,
    observability=None,
    default_scope: MemoryScope | None = None,
    freeze: bool = True,
) -> KnowledgeRuntime | None:
    cfg = knowledge_config(env)
    if not cfg["enabled"]:
        return None
    scope = default_scope or MemoryScope(scope_type=SCOPE_WORKSPACE, scope_id="default")
    registry = KnowledgeSourceRegistry(max_sources=cfg["max_sources"])
    access = KnowledgeAccessPolicy()
    validator = KnowledgeValidator()
    write_policy = KnowledgeWritePolicy()
    rag_builder = RAGContextBuilder()
    service = KnowledgeService(
        registry,
        access=access,
        validator=validator,
        write_policy=write_policy,
        rag_builder=rag_builder,
        memory_service=memory_service,
        document_service=document_service,
        tool_gateway=tool_gateway,
        observability=observability,
        max_item_bytes=cfg["max_item_bytes"],
        max_results=cfg["max_results"],
        max_context_bytes=cfg["max_context_bytes"],
        enabled=True,
    )
    stamp = utc_now()
    ttl = FreshnessPolicy(policy="ttl", ttl_seconds=cfg["default_ttl_seconds"])
    # First-class local adapters — no network
    if memory_service is not None:
        service.register_source(
            KnowledgeSource(
                source_id="memory.default",
                scope=scope,
                source_type=SOURCE_MEMORY,
                name="Memory Knowledge",
                trust_level=TRUST_VALIDATED_INTERNAL,
                refresh_policy=FreshnessPolicy(policy="static"),
                freshness_ttl=None,
                created_at=stamp,
                updated_at=stamp,
            ),
            adapter=MemoryKnowledgeAdapter(memory_service, source_id="memory.default"),
        )
    if document_service is not None:
        service.register_source(
            KnowledgeSource(
                source_id="document.default",
                scope=scope,
                source_type=SOURCE_DOCUMENT,
                name="Document Knowledge",
                trust_level=TRUST_DOCUMENT,
                refresh_policy=FreshnessPolicy(policy="static"),
                freshness_ttl=None,
                created_at=stamp,
                updated_at=stamp,
            ),
            adapter=DocumentKnowledgeAdapter(document_service, source_id="document.default"),
        )
    if freeze:
        registry.freeze()
    return KnowledgeRuntime(
        service=service,
        registry=registry,
        rag_builder=rag_builder,
        validator=validator,
        write_policy=write_policy,
        enabled=True,
    )
