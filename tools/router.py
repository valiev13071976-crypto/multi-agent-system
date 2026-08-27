"""Deterministic Tool Router — no LLM selection."""

from __future__ import annotations

from dataclasses import dataclass

from tools.errors import (
    ToolDisabledError,
    ToolNotFoundError,
    ToolOperationNotAllowedError,
    ToolPolicyDeniedError,
    ToolUnavailableError,
)
from tools.models import (
    ADAPTER_UNAVAILABLE,
    ToolDescriptor,
    ToolRequest,
    WRITE_TRUST_LEVELS,
)
from tools.registry import ToolRegistry


@dataclass(frozen=True)
class RouteDecision:
    tool_id: str
    descriptor: ToolDescriptor
    explicit: bool
    capability_match: bool


class ToolRouter:
    """Select tool/adapter by explicit id or capability — permissions enforced downstream."""

    def __init__(self, registry: ToolRegistry, *, adapter_health: dict[str, str] | None = None):
        self.registry = registry
        self._adapter_health = dict(adapter_health or {})

    def set_adapter_health(self, adapter_id: str, status: str) -> None:
        self._adapter_health[adapter_id] = status

    def route(
        self,
        request: ToolRequest,
        *,
        capability: str | None = None,
    ) -> RouteDecision:
        explicit = bool(request.tool_id)
        if explicit:
            descriptor = self.registry.get(request.tool_id)
        elif capability:
            matches = self.find_by_capability(capability)
            if not matches:
                raise ToolNotFoundError("tool_not_found")
            descriptor = matches[0]
        else:
            raise ToolNotFoundError("tool_not_found")

        if not descriptor.enabled:
            raise ToolDisabledError()
        if request.operation and request.operation not in descriptor.operations:
            raise ToolOperationNotAllowedError()
        health = self._adapter_health.get(descriptor.adapter_id, "healthy")
        if health == ADAPTER_UNAVAILABLE:
            raise ToolUnavailableError()
        return RouteDecision(
            tool_id=descriptor.tool_id,
            descriptor=descriptor,
            explicit=explicit,
            capability_match=bool(capability),
        )

    def find_by_capability(self, capability: str) -> tuple[ToolDescriptor, ...]:
        cap = str(capability or "").strip()
        if not cap:
            return ()
        out = []
        for desc in self.registry.list_tools():
            if cap in desc.capabilities_required or cap == desc.category:
                health = self._adapter_health.get(desc.adapter_id, "healthy")
                if health != ADAPTER_UNAVAILABLE:
                    out.append(desc)
        return tuple(out)

    def find_by_category(self, category: str) -> tuple[ToolDescriptor, ...]:
        cat = str(category or "").strip()
        return tuple(
            d for d in self.registry.list_tools() if d.category == cat
        )

    def validate_side_effect_policy(self, descriptor: ToolDescriptor) -> None:
        """No silent fallback for side-effect tools."""
        if descriptor.trust_level in WRITE_TRUST_LEVELS and descriptor.read_only:
            raise ToolPolicyDeniedError("tool_policy_denied")

    def validate_startup(self) -> list[str]:
        """Return list of validation errors (empty = ok)."""
        errors: list[str] = []
        seen_versions: dict[str, set[str]] = {}
        for desc in self.registry.list_tools(include_disabled=True):
            if not desc.version:
                errors.append(f"missing_version:{desc.tool_id}")
            if desc.read_only and desc.trust_level in WRITE_TRUST_LEVELS:
                errors.append(f"trust_mismatch:{desc.tool_id}")
            versions = seen_versions.setdefault(desc.tool_id, set())
            if desc.version in versions:
                errors.append(f"duplicate_version:{desc.tool_id}:{desc.version}")
            versions.add(desc.version)
        return errors
