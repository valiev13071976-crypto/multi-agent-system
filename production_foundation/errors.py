"""Production foundation error codes."""


class ProductionFoundationError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(code if not message else f"{code}: {message}")


PF_CONFIG_INVALID = "pf_config_invalid"
PF_STORAGE_UNAVAILABLE = "pf_storage_unavailable"
PF_MIGRATION_FAILED = "pf_migration_failed"
PF_BACKUP_FAILED = "pf_backup_failed"
PF_RESTORE_FAILED = "pf_restore_failed"
PF_BACKUP_CORRUPT = "pf_backup_corrupt"
