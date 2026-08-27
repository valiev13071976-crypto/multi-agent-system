"""Canonical ToolRegistry — no dynamic import/exec, freeze after production start."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from tools.errors import ToolNotFoundError, ToolRegistryConflictError, ToolRegistryFrozenError
from tools.models import (
    TOOL_TRUST_PRIVILEGED,
    WRITE_TRUST_LEVELS,
    ToolDescriptor,
)


@dataclass
class ToolRegistration:
    descriptor: ToolDescriptor
    adapter: object | None = None


class ToolRegistry:
    def __init__(self):
        self._items: dict[str, ToolRegistration] = {}
        self._frozen = False
        self.last_error: str | None = None

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        self._frozen = True

    def register(
        self, descriptor: ToolDescriptor, *, adapter: object | None = None
    ) -> None:
        if self._frozen:
            raise ToolRegistryFrozenError()
        if not isinstance(descriptor, ToolDescriptor):
            raise ValueError("invalid_tool_descriptor")
        if descriptor.tool_id in self._items:
            raise ToolRegistryConflictError()
        self._items[descriptor.tool_id] = ToolRegistration(
            descriptor=descriptor, adapter=adapter
        )

    def get(self, tool_id: str) -> ToolDescriptor:
        row = self._items.get(tool_id)
        if row is None:
            raise ToolNotFoundError()
        return row.descriptor

    def get_registration(self, tool_id: str) -> ToolRegistration:
        row = self._items.get(tool_id)
        if row is None:
            raise ToolNotFoundError()
        return row

    def list_tools(self, *, include_disabled: bool = False) -> tuple[ToolDescriptor, ...]:
        out = []
        for row in self._items.values():
            if not include_disabled and not row.descriptor.enabled:
                continue
            out.append(row.descriptor)
        return tuple(out)

    def list_operations(self, tool_id: str) -> tuple[str, ...]:
        return self.get(tool_id).operations

    def unregister(self, tool_id: str) -> None:
        if self._frozen:
            raise ToolRegistryFrozenError()
        self._items.pop(tool_id, None)

    def find_by_capability(self, capability: str) -> tuple[ToolDescriptor, ...]:
        cap = str(capability or "").strip()
        if not cap:
            return ()
        out = []
        for row in self._items.values():
            desc = row.descriptor
            if not desc.enabled:
                continue
            if cap in desc.capabilities_required or cap == desc.category:
                out.append(desc)
        return tuple(out)

    def find_by_category(self, category: str) -> tuple[ToolDescriptor, ...]:
        cat = str(category or "").strip()
        return tuple(
            row.descriptor
            for row in self._items.values()
            if row.descriptor.enabled and row.descriptor.category == cat
        )

    def validate_startup(self) -> list[str]:
        errors: list[str] = []
        seen_versions: dict[str, set[str]] = {}
        for row in self._items.values():
            desc = row.descriptor
            if not desc.version:
                errors.append(f"missing_version:{desc.tool_id}")
            if desc.read_only and desc.trust_level in WRITE_TRUST_LEVELS:
                errors.append(f"trust_mismatch:{desc.tool_id}")
            versions = seen_versions.setdefault(desc.tool_id, set())
            if desc.version in versions:
                errors.append(f"duplicate_version:{desc.tool_id}:{desc.version}")
            versions.add(desc.version)
        return errors

    def health(self) -> Mapping[str, object]:
        descriptors = [row.descriptor for row in self._items.values()]
        return MappingProxyType(
            {
                "registered_tools": len(descriptors),
                "enabled_tools": sum(1 for d in descriptors if d.enabled),
                "read_only_tools": sum(1 for d in descriptors if d.read_only),
                "write_tools": sum(
                    1 for d in descriptors if d.trust_level in WRITE_TRUST_LEVELS
                ),
                "privileged_tools": sum(
                    1 for d in descriptors if d.trust_level == TOOL_TRUST_PRIVILEGED
                ),
                "registry_frozen": self._frozen,
                "last_error": self.last_error,
            }
        )
