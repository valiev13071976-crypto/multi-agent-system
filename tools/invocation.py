"""Normalized tool invocation envelope."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4

from tools.models import ToolRequest


def _meta(value) -> Mapping[str, object]:
    from autonomy.models import sanitize_metadata

    return MappingProxyType(sanitize_metadata(value))


@dataclass(frozen=True)
class ToolInvocation:
    """Governed invocation envelope bound to ToolRequest + execution context."""

    invocation_id: str
    tool_id: str
    tool_version: str
    operation: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    request_id: str = ""
    workflow_id: str = ""
    task_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    actor_id: str = ""
    idempotency_key: str | None = None
    approval_ref: str = ""
    deadline: datetime | None = None
    execution_id: str = ""
    trace_ref: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "arguments", _meta(self.arguments))
        object.__setattr__(self, "metadata", _meta(self.metadata))


def invocation_from_request(request: ToolRequest, *, invocation_id: str | None = None) -> ToolInvocation:
    meta = dict(request.metadata or {})
    approval_ref = str(meta.get("approval_ref") or meta.get("permit_id") or "")
    return ToolInvocation(
        invocation_id=invocation_id or str(uuid4()),
        tool_id=request.tool_id,
        tool_version=str(getattr(request, "tool_version", "") or ""),
        operation=request.operation,
        arguments=request.arguments,
        request_id=request.request_id,
        workflow_id=request.workflow_id,
        task_id=request.task_id,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        actor_id=request.actor_id,
        idempotency_key=request.idempotency_key,
        approval_ref=approval_ref,
        deadline=getattr(request, "deadline", None),
        execution_id=str(getattr(request, "execution_id", "") or ""),
        trace_ref=str(request.correlation_id or ""),
        metadata=_meta(meta),
    )
