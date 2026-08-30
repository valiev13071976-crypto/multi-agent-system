"""Production foundation alert conditions — feeds Block 15 AlertEngine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from operations_admin.models import ALERT_CRITICAL, ALERT_INFO, ALERT_WARNING


@dataclass(frozen=True)
class FoundationAlert:
    code: str
    severity: str
    message: str
    active: bool
    details: dict[str, Any]


def evaluate_foundation_alerts(
    *,
    config_ok: bool,
    storage_writable: bool,
    database_reachable: bool,
    migration_ok: bool,
    backup_status: str,
    backup_age_hours: float | None,
    backup_stale_hours: int,
    disk_free_bytes: int | None,
    disk_threshold_bytes: int,
) -> list[FoundationAlert]:
    alerts: list[FoundationAlert] = []

    if not config_ok:
        alerts.append(FoundationAlert("READINESS_FAILED", ALERT_CRITICAL, "Production config invalid", True, {}))
    if not storage_writable:
        alerts.append(FoundationAlert("STORAGE_UNAVAILABLE", ALERT_CRITICAL, "Persistent storage unavailable", True, {}))
    if not database_reachable:
        alerts.append(FoundationAlert("DATABASE_UNAVAILABLE", ALERT_CRITICAL, "Authoritative database unreachable", True, {}))
    if not migration_ok:
        alerts.append(FoundationAlert("MIGRATION_FAILED", ALERT_CRITICAL, "Schema migration failed", True, {}))
    if backup_status == "FAILED":
        alerts.append(FoundationAlert("BACKUP_FAILED", ALERT_CRITICAL, "Backup failed", True, {}))
    if backup_age_hours is not None and backup_age_hours > backup_stale_hours:
        alerts.append(
            FoundationAlert(
                "BACKUP_STALE",
                ALERT_WARNING,
                f"Last backup older than {backup_stale_hours}h",
                True,
                {"age_hours": backup_age_hours},
            )
        )
    if disk_free_bytes is not None and disk_free_bytes < disk_threshold_bytes:
        alerts.append(
            FoundationAlert(
                "STORAGE_LOW",
                ALERT_WARNING,
                "Disk free space below threshold",
                True,
                {"free_bytes": disk_free_bytes},
            )
        )

    if config_ok and storage_writable and database_reachable and migration_ok and backup_status != "FAILED":
        alerts.append(FoundationAlert("READINESS_FAILED", ALERT_INFO, "Foundation healthy", False, {}))

    return alerts


def hours_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - then
        return delta.total_seconds() / 3600.0
    except ValueError:
        return None
