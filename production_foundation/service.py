"""Production foundation orchestration service."""

from __future__ import annotations

from production_foundation.alert_sink import AlertSink, build_alert_sink
from production_foundation.alerts import evaluate_foundation_alerts, hours_since
from production_foundation.backup import BackupService
from production_foundation.config import ProductionConfig, resolve_production_config, validate_production_config
from production_foundation.migrations import MigrationReport, run_migrations
from production_foundation.models import BackupManifest
from production_foundation.monitoring import build_monitoring_snapshot
from production_foundation.restore import RestoreService
from production_foundation.secrets import inventory_status
from production_foundation.storage import ensure_storage_roots, inventory_as_dict
from production_foundation.proxy import trusted_public_origin


class ProductionFoundationService:
    def __init__(
        self,
        *,
        config: ProductionConfig | None = None,
        backup_service: BackupService | None = None,
        restore_service: RestoreService | None = None,
        alert_sink: AlertSink | None = None,
        side_effect_connection=None,
        saas_store=None,
        persistence_ready: bool = False,
    ):
        self.config = config or resolve_production_config()
        self.backup_service = backup_service or BackupService(
            backup_root=self.config.backup_root,
            side_effect_db=self.config.side_effect_db_path,
            saas_db=self.config.saas_db_path,
            ops_admin_db=self.config.ops_admin_db_path,
            artifact_roots=(self.config.artifact_root, self.config.export_root),
        )
        self.restore_service = restore_service or RestoreService(target_data_dir=self.config.data_dir)
        self.alert_sink = alert_sink or build_alert_sink()
        self.side_effect_connection = side_effect_connection
        self.saas_store = saas_store
        self.persistence_ready = persistence_ready
        self._migration_report: MigrationReport | None = None

    def initialize(self) -> dict:
        ensure_storage_roots()
        lock = None
        from production_foundation.migrations import MigrationLock

        lock = MigrationLock(self.config.data_dir)
        acquired = lock.acquire(timeout_seconds=30.0)
        try:
            self._migration_report = run_migrations(
                side_effect_db_path=self.config.side_effect_db_path,
                saas_db_path=self.config.saas_db_path,
                side_effect_connection=self.side_effect_connection,
                saas_store=self.saas_store,
            )
        finally:
            if acquired:
                lock.release()
        return self._migration_report.as_dict() if self._migration_report else {}

    def config_report(self) -> dict:
        return validate_production_config().as_dict()

    def storage_inventory(self) -> list[dict]:
        return inventory_as_dict()

    def secret_inventory(self) -> list[dict]:
        import os

        return inventory_status(dict(os.environ))

    def production_status(self) -> dict:
        migration = self._migration_report.overall if self._migration_report else "UNKNOWN"
        last = self.backup_service.last_success
        snap = build_monitoring_snapshot(
            migration_state=migration,
            database_reachable=self.persistence_ready,
            last_backup_at=last.created_at if last else None,
            last_backup_status=last.status if last else "UNKNOWN",
        )
        return {
            "environment": self.config.environment,
            "public_origin": trusted_public_origin(),
            "config": self.config_report(),
            "monitoring": snap.as_dict(),
            "storage_inventory": self.storage_inventory(),
            "secret_inventory_status": "redacted",
            "migration": self._migration_report.as_dict() if self._migration_report else {},
            "last_backup": last.as_dict() if last else None,
        }

    def run_backup(self) -> BackupManifest:
        return self.backup_service.create_backup()

    def restore_backup(self, backup_dir: str) -> dict:
        return self.restore_service.restore(backup_dir)

    def evaluate_and_emit_alerts(self, *, alert_engine=None) -> list[dict]:
        cfg_report = validate_production_config()
        snap = build_monitoring_snapshot(
            migration_state=self._migration_report.overall if self._migration_report else "UNKNOWN",
            database_reachable=self.persistence_ready,
            last_backup_at=self.backup_service.last_success.created_at if self.backup_service.last_success else None,
            last_backup_status=self.backup_service.last_success.status if self.backup_service.last_success else "UNKNOWN",
        )
        age = hours_since(snap.last_backup_at)
        alerts = evaluate_foundation_alerts(
            config_ok=cfg_report.overall != "FAIL",
            storage_writable=snap.storage_writable,
            database_reachable=snap.database_reachable,
            migration_ok=(self._migration_report.overall == "PASS") if self._migration_report else False,
            backup_status=snap.last_backup_status,
            backup_age_hours=age,
            backup_stale_hours=self.config.backup_stale_hours,
            disk_free_bytes=snap.storage_free_bytes,
            disk_threshold_bytes=self.config.disk_free_threshold_bytes,
        )
        emitted = []
        for alert in alerts:
            if alert_engine is not None:
                view = alert_engine.observe(
                    source="production_foundation",
                    message=alert.code,
                    severity=alert.severity,
                    active=alert.active,
                )
                if view:
                    emitted.append(view.__dict__)
            if alert.active:
                self.alert_sink.deliver(
                    code=alert.code,
                    severity=alert.severity,
                    message=alert.message,
                    details=alert.details,
                )
        return emitted
