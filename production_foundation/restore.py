"""Restore service — isolated restore with manifest validation."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from production_foundation.errors import PF_BACKUP_CORRUPT, PF_RESTORE_FAILED, ProductionFoundationError
from production_foundation.models import BackupManifest


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def load_manifest(backup_dir: str) -> BackupManifest:
    path = Path(backup_dir) / "manifest.json"
    if not path.exists():
        raise ProductionFoundationError(PF_BACKUP_CORRUPT, "manifest_missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    return BackupManifest(
        backup_id=data["backup_id"],
        created_at=data["created_at"],
        application_version=data.get("application_version", ""),
        schema_versions=dict(data.get("schema_versions") or {}),
        included_stores=tuple(data.get("included_stores") or ()),
        included_artifact_roots=tuple(data.get("included_artifact_roots") or ()),
        files=tuple(data.get("files") or ()),
        total_bytes=int(data.get("total_bytes") or 0),
        status=data.get("status", ""),
        checksum=data.get("checksum", ""),
    )


def verify_manifest(backup_dir: str) -> BackupManifest:
    manifest = load_manifest(backup_dir)
    base = Path(backup_dir)
    for entry in manifest.files:
        fp = base / entry["path"]
        if not fp.exists():
            raise ProductionFoundationError(PF_BACKUP_CORRUPT, f"missing:{entry['path']}")
        if _sha256_file(fp) != entry["checksum"]:
            raise ProductionFoundationError(PF_BACKUP_CORRUPT, f"checksum:{entry['path']}")
    return manifest


class RestoreService:
    def __init__(self, *, target_data_dir: str):
        self.target_data_dir = target_data_dir

    def restore(self, backup_dir: str) -> dict:
        manifest = verify_manifest(backup_dir)
        target = Path(self.target_data_dir)
        target.mkdir(parents=True, exist_ok=True)
        src = Path(backup_dir)

        db_targets = {
            "side_effects": target / "side_effects.sqlite3",
            "saas_product": target / "saas_product.sqlite",
            "ops_admin": target / "ops_admin.sqlite",
        }
        restored: list[str] = []
        for store in manifest.included_stores:
            src_db = src / "databases" / f"{store}.sqlite3"
            if not src_db.exists():
                continue
            dst = db_targets.get(store)
            if dst is None:
                continue
            shutil.copy2(src_db, dst)
            restored.append(store)

        artifacts_src = src / "artifacts"
        if artifacts_src.exists():
            dst_art = target / "artifacts"
            if dst_art.exists():
                shutil.rmtree(dst_art)
            shutil.copytree(artifacts_src, dst_art)
            restored.append("artifacts")

        return {"status": "SUCCESS", "restored": restored, "backup_id": manifest.backup_id}

    def verify_only(self, backup_dir: str) -> BackupManifest:
        return verify_manifest(backup_dir)
