"""Block 7 platform models — versioned dataset contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata
from security.tenant import normalize_tenant_id, require_tenant_id

PLATFORM_SCHEMA_VERSION = "1.0.0"

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_PARTIAL = "partial"


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value or {}))


@dataclass(frozen=True)
class DatasetRef:
    dataset_id: str
    tenant_id: str
    artifact_id: str = ""
    source_type: str = "upload"
    filename: str = ""
    detected_format: str = ""
    content_hash: str = ""
    byte_size: int = 0
    row_estimate: int = 0
    schema_version: str = PLATFORM_SCHEMA_VERSION
    lineage: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "lineage", _meta(self.lineage))


@dataclass(frozen=True)
class DatasetVersion:
    version_id: str
    dataset_id: str
    tenant_id: str
    parent_version_id: str = ""
    content_hash: str = ""
    row_count: int = 0
    transformation: str = "ingest"
    producing_operation: str = ""
    created_at: datetime | None = None
    lineage: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "lineage", _meta(self.lineage))


@dataclass(frozen=True)
class DatasetProcessingJob:
    job_id: str
    dataset_id: str
    tenant_id: str
    operations: tuple[str, ...]
    workload_class: str
    execution_lane: str
    stage: str = "ingest"
    checkpoint: Mapping[str, object] = field(default_factory=dict)
    status: str = JOB_PENDING
    profile_version: str = PLATFORM_SCHEMA_VERSION
    pinned_profiles: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "operations", tuple(self.operations or ()))
        object.__setattr__(self, "checkpoint", _meta(self.checkpoint))
        object.__setattr__(self, "pinned_profiles", _meta(self.pinned_profiles))


@dataclass(frozen=True)
class GeneratedWorkbook:
    workbook_id: str
    tenant_id: str
    template_id: str
    template_version: str
    filename: str
    content_hash: str
    byte_size: int
    input_versions: tuple[str, ...] = ()
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tenant_id", require_tenant_id(self.tenant_id))
        object.__setattr__(self, "input_versions", tuple(self.input_versions or ()))
        object.__setattr__(self, "provenance", _meta(self.provenance))
