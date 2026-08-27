"""Canonical Source Registry — tenant-scoped acquisition sources."""

from __future__ import annotations

from acquisition.errors import (
    SourceAlreadyRegisteredError,
    SourceNotFoundError,
    SourceRegistryFrozenError,
)
from acquisition.models import SourceDescriptor
from security.tenant import normalize_tenant_id, tenants_match


class SourceRegistry:
    def __init__(self):
        self._items: dict[tuple[str, str], SourceDescriptor] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        self._frozen = True

    def register(self, descriptor: SourceDescriptor) -> None:
        if self._frozen:
            raise SourceRegistryFrozenError()
        if not isinstance(descriptor, SourceDescriptor):
            raise ValueError("invalid_source_descriptor")
        key = (descriptor.tenant_id, descriptor.source_id)
        if key in self._items:
            raise SourceAlreadyRegisteredError()
        self._items[key] = descriptor

    def get(self, source_id: str, *, tenant_id: str) -> SourceDescriptor:
        tid = normalize_tenant_id(tenant_id)
        row = self._items.get((tid, source_id))
        if row is None:
            raise SourceNotFoundError()
        return row

    def enable(self, source_id: str, *, tenant_id: str, enabled: bool = True) -> SourceDescriptor:
        if self._frozen:
            raise SourceRegistryFrozenError()
        current = self.get(source_id, tenant_id=tenant_id)
        updated = SourceDescriptor(
            source_id=current.source_id,
            source_type=current.source_type,
            tenant_id=current.tenant_id,
            trust_level=current.trust_level,
            freshness_policy=current.freshness_policy,
            tool_id=current.tool_id,
            integration_id=current.integration_id,
            enabled=bool(enabled),
            name=current.name,
            allowed_domains=current.allowed_domains,
            metadata=dict(current.metadata),
        )
        self._items[(updated.tenant_id, updated.source_id)] = updated
        return updated

    def list_sources(
        self, *, tenant_id: str, include_disabled: bool = False
    ) -> tuple[SourceDescriptor, ...]:
        tid = normalize_tenant_id(tenant_id)
        out = []
        for (t, _), desc in self._items.items():
            if not tenants_match(t, tid):
                continue
            if not include_disabled and not desc.enabled:
                continue
            out.append(desc)
        return tuple(out)

    def assert_tenant(self, source_id: str, *, tenant_id: str) -> SourceDescriptor:
        return self.get(source_id, tenant_id=tenant_id)
