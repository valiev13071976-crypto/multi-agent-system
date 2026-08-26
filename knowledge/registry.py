"""Knowledge source registry — freeze after composition."""

from __future__ import annotations

from knowledge.models import (
    KNOWLEDGE_SOURCE_REGISTRY_VERSION,
    KnowledgeSource,
)


class KnowledgeSourceRegistryError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class KnowledgeSourceRegistry:
    registry_version = KNOWLEDGE_SOURCE_REGISTRY_VERSION

    def __init__(self, *, max_sources: int = 64):
        self._sources: dict[str, KnowledgeSource] = {}
        self._adapters: dict[str, object] = {}
        self._frozen = False
        self.max_sources = int(max_sources)

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, source: KnowledgeSource, adapter=None) -> KnowledgeSource:
        if self._frozen:
            raise KnowledgeSourceRegistryError("registry_frozen")
        if source.source_id in self._sources:
            raise KnowledgeSourceRegistryError("duplicate_source")
        if len(self._sources) >= self.max_sources:
            raise KnowledgeSourceRegistryError("max_sources_exceeded")
        self._sources[source.source_id] = source
        if adapter is not None:
            self._adapters[source.source_id] = adapter
        return source

    def get(self, source_id: str) -> KnowledgeSource:
        row = self._sources.get(source_id)
        if row is None:
            raise KnowledgeSourceRegistryError("unknown_source")
        return row

    def get_adapter(self, source_id: str):
        return self._adapters.get(source_id)

    def list_sources(self, *, enabled_only: bool = False) -> tuple[KnowledgeSource, ...]:
        rows = list(self._sources.values())
        if enabled_only:
            rows = [r for r in rows if r.enabled]
        return tuple(sorted(rows, key=lambda s: s.source_id))

    def enable(self, source_id: str) -> KnowledgeSource:
        if self._frozen:
            raise KnowledgeSourceRegistryError("registry_frozen")
        src = self.get(source_id)
        updated = KnowledgeSource(
            source_id=src.source_id,
            scope=src.scope,
            source_type=src.source_type,
            name=src.name,
            trust_level=src.trust_level,
            enabled=True,
            refresh_policy=src.refresh_policy,
            freshness_ttl=src.freshness_ttl,
            created_at=src.created_at,
            updated_at=src.updated_at,
            version=src.version + 1,
            metadata_safe=dict(src.metadata_safe),
        )
        self._sources[source_id] = updated
        return updated

    def disable(self, source_id: str) -> KnowledgeSource:
        # Allow disable even when frozen (operational control without re-register).
        src = self.get(source_id)
        updated = KnowledgeSource(
            source_id=src.source_id,
            scope=src.scope,
            source_type=src.source_type,
            name=src.name,
            trust_level=src.trust_level,
            enabled=False,
            refresh_policy=src.refresh_policy,
            freshness_ttl=src.freshness_ttl,
            created_at=src.created_at,
            updated_at=src.updated_at,
            version=src.version + 1,
            metadata_safe=dict(src.metadata_safe),
        )
        self._sources[source_id] = updated
        return updated

    def freeze(self) -> None:
        self._frozen = True


def source_registry_snapshot(registry: KnowledgeSourceRegistry | None = None) -> dict:
    reg = registry or KnowledgeSourceRegistry()
    return {
        "knowledge_source_registry_version": KNOWLEDGE_SOURCE_REGISTRY_VERSION,
        "source_count": len(reg.list_sources()),
        "frozen": reg.frozen,
        "source_types_supported": [
            "memory",
            "document",
            "local_file",
            "read_only_external",
            "search_provider",
            "manual_reference",
        ],
    }
