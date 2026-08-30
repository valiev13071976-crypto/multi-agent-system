"""Authoritative storage inventory and path resolution."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from production_foundation.config import resolve_production_config
from production_foundation.models import StoreInventoryEntry

STORE_INVENTORY: tuple[StoreInventoryEntry, ...] = (
    StoreInventoryEntry(
        name="side_effects",
        purpose="Workflow, task queue, side effects, approvals, governor",
        technology="sqlite",
        path_key="SIDE_EFFECT_DB_PATH",
        authoritative=True,
        tenant_partitioned=True,
        backup_required=True,
        migration_key="side_effects",
    ),
    StoreInventoryEntry(
        name="saas_product",
        purpose="Tenants, memberships, billing, privacy jobs",
        technology="sqlite",
        path_key="SAAS_PRODUCT_DB_PATH",
        authoritative=True,
        tenant_partitioned=True,
        backup_required=True,
        migration_key="saas_product",
    ),
    StoreInventoryEntry(
        name="ops_admin",
        purpose="Operations admin audit",
        technology="sqlite",
        path_key="OPS_ADMIN_DB_PATH",
        authoritative=True,
        tenant_partitioned=False,
        backup_required=True,
        migration_key="ops_admin",
    ),
    StoreInventoryEntry(
        name="artifacts",
        purpose="Governed generated artifacts and uploads",
        technology="filesystem",
        path_key="PANDA_ARTIFACT_ROOT",
        authoritative=True,
        tenant_partitioned=True,
        backup_required=True,
    ),
    StoreInventoryEntry(
        name="privacy_exports",
        purpose="Privacy export artifacts",
        technology="filesystem",
        path_key="SAAS_EXPORT_ROOT",
        authoritative=True,
        tenant_partitioned=True,
        backup_required=True,
    ),
)


def resolve_store_paths(env: dict | None = None) -> dict[str, str]:
    cfg = resolve_production_config(env)
    return {
        "PANDA_DATA_DIR": cfg.data_dir,
        "SIDE_EFFECT_DB_PATH": cfg.side_effect_db_path,
        "SAAS_PRODUCT_DB_PATH": cfg.saas_db_path,
        "OPS_ADMIN_DB_PATH": cfg.ops_admin_db_path,
        "PANDA_ARTIFACT_ROOT": cfg.artifact_root,
        "SAAS_EXPORT_ROOT": cfg.export_root,
        "PANDA_BACKUP_ROOT": cfg.backup_root,
    }


def ensure_storage_roots(env: dict | None = None) -> dict[str, bool]:
    cfg = resolve_production_config(env)
    results: dict[str, bool] = {}
    for path in (cfg.data_dir, cfg.artifact_root, cfg.backup_root, cfg.export_root):
        p = Path(path)
        try:
            p.mkdir(parents=True, exist_ok=True)
            test = p / ".write_probe"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
            results[str(p)] = True
        except OSError:
            results[str(p)] = False
    db_parent = Path(cfg.side_effect_db_path).parent
    db_parent.mkdir(parents=True, exist_ok=True)
    results[str(db_parent)] = db_parent.exists()
    return results


def disk_usage(path: str) -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage(path)
        return int(usage.free), int(usage.used)
    except OSError:
        return None, None


def inventory_as_dict(env: dict | None = None) -> list[dict]:
    paths = resolve_store_paths(env)
    out = []
    for entry in STORE_INVENTORY:
        out.append(
            {
                "name": entry.name,
                "purpose": entry.purpose,
                "technology": entry.technology,
                "path": paths.get(entry.path_key, ""),
                "authoritative": entry.authoritative,
                "tenant_partitioned": entry.tenant_partitioned,
                "backup_required": entry.backup_required,
                "migration_key": entry.migration_key,
            }
        )
    return out
