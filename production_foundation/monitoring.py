"""Production foundation monitoring snapshot."""

from __future__ import annotations

from production_foundation.config import resolve_production_config, validate_production_config
from production_foundation.models import FoundationMonitoringSnapshot
from production_foundation.storage import disk_usage, ensure_storage_roots


def build_monitoring_snapshot(
    *,
    env: dict | None = None,
    migration_state: str = "UNKNOWN",
    database_reachable: bool = False,
    last_backup_at: str | None = None,
    last_backup_status: str = "UNKNOWN",
) -> FoundationMonitoringSnapshot:
    cfg = resolve_production_config(env)
    writable_map = ensure_storage_roots(env)
    storage_writable = all(writable_map.values()) if writable_map else False
    free, used = disk_usage(cfg.data_dir)
    config_report = validate_production_config(env)
    disk_status = "UNKNOWN"
    if free is not None:
        disk_status = "LOW" if free < cfg.disk_free_threshold_bytes else "OK"

    components = [
        {"name": "config", "status": config_report.overall},
        {"name": "storage", "status": "PASS" if storage_writable else "FAIL"},
        {"name": "database", "status": "PASS" if database_reachable else "FAIL"},
        {"name": "migration", "status": migration_state},
        {"name": "backup", "status": last_backup_status},
    ]

    return FoundationMonitoringSnapshot(
        storage_root=cfg.data_dir,
        storage_writable=storage_writable,
        storage_free_bytes=free,
        storage_used_bytes=used,
        database_reachable=database_reachable,
        migration_state=migration_state,
        last_backup_at=last_backup_at,
        last_backup_status=last_backup_status,
        backup_destination_configured=cfg.backup_destination not in {"", "local"} or cfg.environment != "production",
        alert_sink_configured=bool(cfg.alert_webhook_url),
        disk_threshold_status=disk_status,
        components=components,
    )
