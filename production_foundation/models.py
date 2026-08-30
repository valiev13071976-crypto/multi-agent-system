"""Production foundation domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StoreInventoryEntry:
    name: str
    purpose: str
    technology: str
    path_key: str
    authoritative: bool
    tenant_partitioned: bool
    backup_required: bool
    migration_key: str | None = None


@dataclass
class BackupManifest:
    backup_id: str
    created_at: str
    application_version: str
    schema_versions: dict[str, int | str]
    included_stores: tuple[str, ...]
    included_artifact_roots: tuple[str, ...]
    files: tuple[dict[str, Any], ...]
    total_bytes: int
    status: str
    checksum: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "application_version": self.application_version,
            "schema_versions": dict(self.schema_versions),
            "included_stores": list(self.included_stores),
            "included_artifact_roots": list(self.included_artifact_roots),
            "files": [dict(f) for f in self.files],
            "total_bytes": self.total_bytes,
            "status": self.status,
            "checksum": self.checksum,
        }


@dataclass
class MigrationReport:
    overall: str
    stores: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"overall": self.overall, "stores": list(self.stores)}


@dataclass
class FoundationMonitoringSnapshot:
    storage_root: str
    storage_writable: bool
    storage_free_bytes: int | None
    storage_used_bytes: int | None
    database_reachable: bool
    migration_state: str
    last_backup_at: str | None
    last_backup_status: str
    backup_destination_configured: bool
    alert_sink_configured: bool
    disk_threshold_status: str
    components: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "storage_root": self.storage_root,
            "storage_writable": self.storage_writable,
            "storage_free_bytes": self.storage_free_bytes,
            "storage_used_bytes": self.storage_used_bytes,
            "database_reachable": self.database_reachable,
            "migration_state": self.migration_state,
            "last_backup_at": self.last_backup_at,
            "last_backup_status": self.last_backup_status,
            "backup_destination_configured": self.backup_destination_configured,
            "alert_sink_configured": self.alert_sink_configured,
            "disk_threshold_status": self.disk_threshold_status,
            "components": list(self.components),
        }
