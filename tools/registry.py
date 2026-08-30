"""Canonical ToolRegistry — no dynamic import/exec, freeze after production start."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping

from tools.errors import (
    ToolDisabledError,
    ToolNotFoundError,
    ToolRegistryConflictError,
    ToolRegistryFrozenError,
)
from tools.models import (
    TOOL_TRUST_PRIVILEGED,
    VERSION_ACTIVE,
    VERSION_DISABLED,
    WRITE_TRUST_LEVELS,
    ToolDescriptor,
)


@dataclass
class ToolRegistration:
    descriptor: ToolDescriptor
    adapter: object | None = None


def _version_key(tool_id: str, version: str) -> str:
    return f"{tool_id}@{version}"


class ToolRegistry:
    def __init__(self):
        self._items: dict[str, ToolRegistration] = {}
        # tool_id -> version -> registration
        self._versions: dict[str, dict[str, ToolRegistration]] = {}
        self._frozen = False
        self.last_error: str | None = None

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        self._frozen = True

    def register(
        self,
        descriptor: ToolDescriptor,
        *,
        adapter: object | None = None,
        as_primary: bool | None = None,
    ) -> None:
        if self._frozen:
            raise ToolRegistryFrozenError()
        if not isinstance(descriptor, ToolDescriptor):
            raise ValueError("invalid_tool_descriptor")
        tool_id = descriptor.tool_id
        version = descriptor.version
        versions = self._versions.setdefault(tool_id, {})
        if version in versions:
            raise ToolRegistryConflictError()
        # Primary key conflict only when registering a second distinct primary
        # without versioning intent — first registration owns primary slot.
        if tool_id in self._items and version == self._items[tool_id].descriptor.version:
            raise ToolRegistryConflictError()
        row = ToolRegistration(descriptor=descriptor, adapter=adapter)
        versions[version] = row
        make_primary = as_primary
        if make_primary is None:
            make_primary = tool_id not in self._items or (
                descriptor.enabled
                and descriptor.version_status == VERSION_ACTIVE
                and (
                    not self._items[tool_id].descriptor.enabled
                    or self._items[tool_id].descriptor.version_status != VERSION_ACTIVE
                )
            )
        if make_primary or tool_id not in self._items:
            self._items[tool_id] = row

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

    def resolve(self, tool_id: str, version: str | None = None) -> ToolRegistration:
        """Fail-closed resolve by tool_id and optional version pin."""
        tid = str(tool_id or "").strip()
        if not tid:
            raise ToolNotFoundError("tool_not_found")
        pin = str(version or "").strip() or None
        if pin:
            versions = self._versions.get(tid) or {}
            row = versions.get(pin)
            if row is None:
                # Also allow explicit tool_id@version primary key style
                row = self._items.get(_version_key(tid, pin))
            if row is None:
                self.last_error = "tool_version_not_found"
                raise ToolNotFoundError("tool_version_not_found")
        else:
            row = self._items.get(tid)
            if row is None:
                self.last_error = "tool_not_found"
                raise ToolNotFoundError("tool_not_found")
        desc = row.descriptor
        if not desc.enabled or desc.version_status == VERSION_DISABLED:
            self.last_error = "tool_disabled"
            raise ToolDisabledError("tool_disabled")
        return row

    def list_versions(self, tool_id: str) -> tuple[str, ...]:
        versions = self._versions.get(tool_id) or {}
        if not versions and tool_id in self._items:
            return (self._items[tool_id].descriptor.version,)
        return tuple(sorted(versions.keys()))

    def enable(self, tool_id: str, *, version: str | None = None) -> None:
        self._set_enabled(tool_id, True, version=version)

    def disable(self, tool_id: str, *, version: str | None = None) -> None:
        self._set_enabled(tool_id, False, version=version)

    def _set_enabled(
        self, tool_id: str, enabled: bool, *, version: str | None = None
    ) -> None:
        if self._frozen:
            raise ToolRegistryFrozenError()
        pin = str(version or "").strip() or None
        if pin:
            versions = self._versions.get(tool_id) or {}
            row = versions.get(pin)
            if row is None:
                raise ToolNotFoundError("tool_version_not_found")
        else:
            row = self._items.get(tool_id)
            if row is None:
                raise ToolNotFoundError()
        new_status = row.descriptor.version_status
        if enabled and new_status == VERSION_DISABLED:
            new_status = VERSION_ACTIVE
        if not enabled:
            new_status = VERSION_DISABLED
        new_desc = replace(
            row.descriptor,
            enabled=enabled,
            version_status=new_status,
        )
        new_row = ToolRegistration(descriptor=new_desc, adapter=row.adapter)
        versions = self._versions.setdefault(tool_id, {})
        versions[new_desc.version] = new_row
        primary = self._items.get(tool_id)
        if primary is None or primary.descriptor.version == new_desc.version:
            self._items[tool_id] = new_row

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
        self._versions.pop(tool_id, None)

    def find_by_capability(self, capability: str) -> tuple[ToolDescriptor, ...]:
        cap = str(capability or "").strip()
        if not cap:
            return ()
        out = []
        for row in self._items.values():
            desc = row.descriptor
            if not desc.enabled:
                continue
            if desc.version_status == VERSION_DISABLED:
                continue
            if cap in desc.capabilities_required or cap == desc.category:
                out.append(desc)
        return tuple(out)

    def find_by_category(self, category: str) -> tuple[ToolDescriptor, ...]:
        cat = str(category or "").strip()
        return tuple(
            row.descriptor
            for row in self._items.values()
            if row.descriptor.enabled
            and row.descriptor.version_status != VERSION_DISABLED
            and row.descriptor.category == cat
        )

    def validate_startup(self) -> list[str]:
        errors: list[str] = []
        seen_versions: dict[str, set[str]] = {}
        for tool_id, versions in self._versions.items():
            for version, row in versions.items():
                desc = row.descriptor
                if not desc.version:
                    errors.append(f"missing_version:{desc.tool_id}")
                if desc.read_only and desc.trust_level in WRITE_TRUST_LEVELS:
                    errors.append(f"trust_mismatch:{desc.tool_id}")
                bucket = seen_versions.setdefault(desc.tool_id, set())
                if desc.version in bucket:
                    errors.append(f"duplicate_version:{desc.tool_id}:{desc.version}")
                bucket.add(desc.version)
        # Also scan primary-only entries
        for row in self._items.values():
            desc = row.descriptor
            if desc.tool_id not in seen_versions:
                if not desc.version:
                    errors.append(f"missing_version:{desc.tool_id}")
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
