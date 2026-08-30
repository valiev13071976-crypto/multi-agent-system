"""Migration registry — deterministic schema compatibility checks."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from production_foundation.models import MigrationReport

SIDE_EFFECT_SCHEMA_VERSION = 8
SAAS_SCHEMA_VERSION = 1


def _read_side_effect_version(db_path: str) -> int | None:
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version FROM side_effect_schema_meta WHERE id=1").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _read_saas_version(db_path: str) -> int | None:
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM saas_schema_meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def run_migrations(
    *,
    side_effect_db_path: str,
    saas_db_path: str,
    side_effect_connection=None,
    saas_store=None,
) -> MigrationReport:
    """Apply/verify schemas via existing store initializers."""

    stores: list[dict] = []
    overall = "PASS"

    if side_effect_connection is not None:
        try:
            version = side_effect_connection.initialize_schema()
            stores.append({"store": "side_effects", "status": "PASS", "version": version})
        except Exception as exc:
            overall = "FAIL"
            stores.append({"store": "side_effects", "status": "FAIL", "error": type(exc).__name__})
    elif Path(side_effect_db_path).exists():
        ver = _read_side_effect_version(side_effect_db_path)
        if ver is not None and ver > SIDE_EFFECT_SCHEMA_VERSION:
            overall = "FAIL"
            stores.append({"store": "side_effects", "status": "FAIL", "error": "schema_newer_than_app"})
        else:
            stores.append({"store": "side_effects", "status": "PASS", "version": ver})

    if saas_store is not None:
        try:
            ver = getattr(saas_store, "SCHEMA_VERSION", SAAS_SCHEMA_VERSION)
            stores.append({"store": "saas_product", "status": "PASS", "version": ver})
        except Exception as exc:
            overall = "FAIL"
            stores.append({"store": "saas_product", "status": "FAIL", "error": type(exc).__name__})
    elif Path(saas_db_path).exists():
        ver = _read_saas_version(saas_db_path)
        if ver is not None and ver > SAAS_SCHEMA_VERSION:
            overall = "FAIL"
            stores.append({"store": "saas_product", "status": "FAIL", "error": "schema_newer_than_app"})
        else:
            stores.append({"store": "saas_product", "status": "PASS", "version": ver})

    return MigrationReport(overall=overall, stores=stores)


def migration_lock_path(data_dir: str) -> str:
    return str(Path(data_dir) / ".migration.lock")


class MigrationLock:
    """File-based migration lock for concurrent startup safety."""

    def __init__(self, data_dir: str):
        self.path = migration_lock_path(data_dir)
        self._fd = None

    def acquire(self, timeout_seconds: float = 30.0) -> bool:
        import time

        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                self._fd = fd
                return True
            except FileExistsError:
                time.sleep(0.05)
        return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            Path(self.path).unlink(missing_ok=True)
        except OSError:
            pass
