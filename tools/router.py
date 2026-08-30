"""Deterministic Tool Router — no LLM selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from tools.errors import (
    ToolDisabledError,
    ToolNotFoundError,
    ToolOperationNotAllowedError,
    ToolPolicyDeniedError,
    ToolUnavailableError,
)
from tools.models import (
    ADAPTER_UNAVAILABLE,
    VERSION_DISABLED,
    ToolDescriptor,
    ToolRequest,
    WRITE_TRUST_LEVELS,
)
from tools.registry import ToolRegistry


@dataclass(frozen=True)
class RouteRejection:
    tool_id: str
    reason: str

    def as_dict(self) -> dict:
        return {"tool_id": self.tool_id, "reason": self.reason}


@dataclass(frozen=True)
class RouteDecision:
    tool_id: str
    descriptor: ToolDescriptor
    explicit: bool
    capability_match: bool
    selected_tool: str = ""
    selected_version: str = ""
    candidates: tuple[str, ...] = ()
    rejected: tuple[RouteRejection, ...] = ()
    policy_decision: str = "allow"
    trust_decision: str = "allow"
    routing_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        selected = self.selected_tool or self.tool_id
        object.__setattr__(self, "selected_tool", selected)
        object.__setattr__(
            self,
            "selected_version",
            self.selected_version or self.descriptor.version,
        )
        object.__setattr__(self, "candidates", tuple(self.candidates or ()))
        object.__setattr__(self, "rejected", tuple(self.rejected or ()))
        object.__setattr__(
            self,
            "routing_metadata",
            MappingProxyType(dict(self.routing_metadata or {})),
        )


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
        capabilities=None,
        workload_class: str | None = None,
        data_scope: str | None = None,
        require_tenant_permission: bool = False,
    ) -> RouteDecision:
        explicit = bool(request.tool_id)
        rejected: list[RouteRejection] = []
        candidates: list[str] = []
        meta: dict[str, object] = {}

        if explicit:
            version_pin = str(getattr(request, "tool_version", "") or "").strip() or None
            try:
                row = self.registry.resolve(request.tool_id, version_pin)
                descriptor = row.descriptor
            except ToolDisabledError:
                rejected.append(RouteRejection(request.tool_id, "tool_disabled"))
                raise
            except ToolNotFoundError as exc:
                reason = getattr(exc, "error_code", "tool_not_found")
                rejected.append(RouteRejection(request.tool_id, reason))
                raise
            candidates = [descriptor.tool_id]
        elif capability:
            matches, rejected = self._eligible_by_capability(
                capability,
                operation=request.operation,
                capabilities=capabilities,
                workload_class=workload_class or getattr(request, "metadata", {}).get(  # type: ignore[union-attr]
                    "workload_class"
                ),
                data_scope=data_scope or getattr(request, "data_scope_ref", "") or None,
            )
            candidates = [d.tool_id for d in matches]
            if not matches:
                meta["rejected"] = [r.as_dict() for r in rejected]
                raise ToolNotFoundError("no_eligible_tool")
            descriptor = matches[0]
        else:
            raise ToolNotFoundError("tool_not_found")

        if not descriptor.enabled or descriptor.version_status == VERSION_DISABLED:
            rejected.append(RouteRejection(descriptor.tool_id, "tool_disabled"))
            raise ToolDisabledError()
        if request.operation and request.operation not in descriptor.operations:
            rejected.append(RouteRejection(descriptor.tool_id, "operation_not_allowed"))
            raise ToolOperationNotAllowedError()

        # Tenant permission via capabilities (no silent unauthorized fallback)
        policy_decision = "allow"
        trust_decision = "allow"
        if capabilities is not None or require_tenant_permission:
            ok, reason = self._tenant_capability_ok(descriptor, capabilities)
            if not ok:
                policy_decision = "deny"
                rejected.append(RouteRejection(descriptor.tool_id, reason))
                raise ToolPolicyDeniedError(reason)

        # Trust decision is recorded; health never overrides auth
        if descriptor.trust_level in WRITE_TRUST_LEVELS and descriptor.read_only:
            trust_decision = "deny"
            rejected.append(RouteRejection(descriptor.tool_id, "trust_policy_denied"))
            raise ToolPolicyDeniedError("tool_policy_denied")

        health = self._adapter_health.get(descriptor.adapter_id, "healthy")
        meta["adapter_health"] = health
        if health == ADAPTER_UNAVAILABLE:
            rejected.append(RouteRejection(descriptor.tool_id, "adapter_unavailable"))
            raise ToolUnavailableError()

        # Workload / data scope soft filters already applied for capability route;
        # for explicit route record mismatch metadata without silent swap.
        hint = str(descriptor.workload_class_hint or "")
        if workload_class and hint and hint != workload_class:
            meta["workload_mismatch"] = True
        if data_scope and descriptor.resource_prefix:
            if not str(data_scope).startswith(str(descriptor.resource_prefix)):
                meta["data_scope_mismatch"] = True

        return RouteDecision(
            tool_id=descriptor.tool_id,
            descriptor=descriptor,
            explicit=explicit,
            capability_match=bool(capability),
            selected_tool=descriptor.tool_id,
            selected_version=descriptor.version,
            candidates=tuple(candidates),
            rejected=tuple(rejected),
            policy_decision=policy_decision,
            trust_decision=trust_decision,
            routing_metadata=meta,
        )

    def _tenant_capability_ok(self, descriptor: ToolDescriptor, capabilities) -> tuple[bool, str]:
        required = set(descriptor.capabilities_required or ())
        if not required:
            return True, "allow"
        if capabilities is None:
            return False, "missing_tool_capability"
        provided: set[str] = set()
        if hasattr(capabilities, "capabilities"):
            provided |= set(capabilities.capabilities or ())
        elif isinstance(capabilities, (set, list, tuple)):
            provided |= set(capabilities)
        if not required <= provided:
            return False, "missing_tool_capability"
        return True, "allow"

    def _eligible_by_capability(
        self,
        capability: str,
        *,
        operation: str = "",
        capabilities=None,
        workload_class: str | None = None,
        data_scope: str | None = None,
    ) -> tuple[list[ToolDescriptor], list[RouteRejection]]:
        rejected: list[RouteRejection] = []
        eligible: list[ToolDescriptor] = []
        for desc in self.registry.list_tools(include_disabled=True):
            if capability not in desc.capabilities_required and capability != desc.category:
                continue
            if not desc.enabled or desc.version_status == VERSION_DISABLED:
                rejected.append(RouteRejection(desc.tool_id, "tool_disabled"))
                continue
            if operation and operation not in desc.operations:
                rejected.append(RouteRejection(desc.tool_id, "operation_not_allowed"))
                continue
            ok, reason = self._tenant_capability_ok(desc, capabilities) if capabilities is not None else (True, "allow")
            if capabilities is not None and not ok:
                rejected.append(RouteRejection(desc.tool_id, reason))
                continue
            health = self._adapter_health.get(desc.adapter_id, "healthy")
            if health == ADAPTER_UNAVAILABLE:
                rejected.append(RouteRejection(desc.tool_id, "adapter_unavailable"))
                continue
            if workload_class and desc.workload_class_hint:
                if desc.workload_class_hint != workload_class:
                    rejected.append(RouteRejection(desc.tool_id, "workload_incompatible"))
                    continue
            if data_scope and desc.resource_prefix:
                if not str(data_scope).startswith(str(desc.resource_prefix)):
                    rejected.append(RouteRejection(desc.tool_id, "data_scope_mismatch"))
                    continue
            eligible.append(desc)
        return eligible, rejected

    def find_by_capability(self, capability: str) -> tuple[ToolDescriptor, ...]:
        matches, _ = self._eligible_by_capability(capability)
        return tuple(matches)

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
