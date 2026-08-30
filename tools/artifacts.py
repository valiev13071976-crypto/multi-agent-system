"""Artifact references with tenant isolation (fail closed cross-tenant)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata, utc_now
from tools.errors import ToolPermissionDeniedError


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    tenant_id: str
    owner_id: str
    execution_id: str = ""
    workflow_id: str = ""
    tool_id: str = ""
    tool_version: str = ""
    content_type: str = "application/octet-stream"
    provenance: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime | None = None

    def __post_init__(self):
        if not str(self.artifact_id or "").strip():
            raise ValueError("artifact_id_required")
        if not str(self.tenant_id or "").strip():
            raise ValueError("tenant_id_required")
        object.__setattr__(self, "provenance", MappingProxyType(sanitize_metadata(self.provenance)))
        if self.created_at is None:
            object.__setattr__(self, "created_at", utc_now())

    def as_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "tenant_id": self.tenant_id,
            "owner_id": self.owner_id,
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
            "content_type": self.content_type,
            "provenance": dict(self.provenance),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        owner_id: str,
        execution_id: str = "",
        workflow_id: str = "",
        tool_id: str = "",
        tool_version: str = "",
        content_type: str = "application/octet-stream",
        provenance: Mapping[str, object] | None = None,
    ) -> "ArtifactRef":
        return cls(
            artifact_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            owner_id=owner_id,
            execution_id=execution_id,
            workflow_id=workflow_id,
            tool_id=tool_id,
            tool_version=tool_version,
            content_type=content_type,
            provenance=dict(provenance or {}),
        )


class ArtifactStore:
    """In-memory artifact index with fail-closed cross-tenant access."""

    def __init__(self):
        self._items: dict[str, ArtifactRef] = {}

    def put(self, ref: ArtifactRef) -> ArtifactRef:
        self._items[ref.artifact_id] = ref
        return ref

    def get(self, artifact_id: str, *, tenant_id: str) -> ArtifactRef:
        ref = self._items.get(artifact_id)
        if ref is None:
            raise ToolPermissionDeniedError("artifact_not_found")
        if ref.tenant_id != str(tenant_id or ""):
            raise ToolPermissionDeniedError("artifact_tenant_denied")
        return ref

    def list_for_tenant(self, tenant_id: str) -> tuple[ArtifactRef, ...]:
        tid = str(tenant_id or "")
        return tuple(r for r in self._items.values() if r.tenant_id == tid)
