"""Backup service — consistent SQLite + artifact backup with manifest."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from production_foundation.errors import PF_BACKUP_FAILED, ProductionFoundationError
from production_foundation.models import BackupManifest


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sqlite_backup(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(source))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


class BackupService:
    APPLICATION_VERSION = "1.0.0"

    def __init__(self, *, backup_root: str, side_effect_db: str, saas_db: str, ops_admin_db: str, artifact_roots: tuple[str, ...]):
        self.backup_root = backup_root
        self.side_effect_db = side_effect_db
        self.saas_db = saas_db
        self.ops_admin_db = ops_admin_db
        self.artifact_roots = artifact_roots
        self._last_success: BackupManifest | None = None
        self._last_failure: str | None = None

    @property
    def last_success(self) -> BackupManifest | None:
        return self._last_success

    def create_backup(self, *, backup_id: str | None = None) -> BackupManifest:
        bid = backup_id or str(uuid.uuid4())
        dest_dir = Path(self.backup_root) / bid
        if dest_dir.exists():
            raise ProductionFoundationError(PF_BACKUP_FAILED, "backup_id_exists")
        try:
            dest_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            self._last_failure = type(exc).__name__
            raise ProductionFoundationError(PF_BACKUP_FAILED, "destination_unwritable") from exc

        files: list[dict] = []
        schema_versions: dict[str, int | str] = {}
        included_stores: list[str] = []
        included_artifacts: list[str] = []

        db_map = {
            "side_effects": self.side_effect_db,
            "saas_product": self.saas_db,
            "ops_admin": self.ops_admin_db,
        }
        for name, src in db_map.items():
            src_path = Path(src)
            if not src_path.exists():
                continue
            rel = f"databases/{name}.sqlite3"
            dst = dest_dir / rel
            _sqlite_backup(src_path, dst)
            checksum = _sha256_file(dst)
            files.append({"path": rel, "checksum": checksum, "bytes": dst.stat().st_size})
            included_stores.append(name)
            schema_versions[name] = "current"

        for root in self.artifact_roots:
            src_root = Path(root)
            if not src_root.exists():
                continue
            dst_root = dest_dir / "artifacts"
            if src_root.is_dir():
                shutil.copytree(src_root, dst_root, dirs_exist_ok=True)
            included_artifacts.append("artifacts")
            for fp in dst_root.rglob("*"):
                if fp.is_file():
                    rel = str(fp.relative_to(dest_dir)).replace("\\", "/")
                    files.append({"path": rel, "checksum": _sha256_file(fp), "bytes": fp.stat().st_size})

        total = sum(int(f["bytes"]) for f in files)
        manifest = BackupManifest(
            backup_id=bid,
            created_at=_utc(),
            application_version=self.APPLICATION_VERSION,
            schema_versions=schema_versions,
            included_stores=tuple(included_stores),
            included_artifact_roots=tuple(included_artifacts),
            files=tuple(files),
            total_bytes=total,
            status="SUCCESS",
        )
        payload = json.dumps(manifest.as_dict(), sort_keys=True).encode()
        manifest.checksum = hashlib.sha256(payload).hexdigest()
        (dest_dir / "manifest.json").write_text(json.dumps(manifest.as_dict(), indent=2), encoding="utf-8")
        self._last_success = manifest
        self._last_failure = None
        return manifest

    def create_backup_safe(self) -> BackupManifest | None:
        try:
            return self.create_backup()
        except ProductionFoundationError:
            return None
