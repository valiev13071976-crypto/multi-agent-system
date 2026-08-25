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
