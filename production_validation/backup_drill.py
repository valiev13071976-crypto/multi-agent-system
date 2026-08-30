"""Backup/restore drill harness."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

from production_foundation.backup import BackupService
from production_foundation.errors import ProductionFoundationError
from production_foundation.restore import RestoreService, verify_manifest
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass


class BackupDrillHarness:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    def run_isolated(self) -> dict:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            data = Path(tmp) / "data"
            backup = Path(tmp) / "backups"
            recovery = Path(tmp) / "recovery"
            data.mkdir()
            se_db = data / "side_effects.sqlite3"
            saas_db = data / "saas.sqlite"
            ops_db = data / "ops.sqlite"
            art = data / "artifacts"
            art.mkdir()
            conn = sqlite3.connect(se_db)
            conn.execute("CREATE TABLE stage3_marker (id TEXT PRIMARY KEY, checksum TEXT)")
            marker = f"stage3-{uuid.uuid4().hex[:8]}"
            checksum = hashlib.sha256(marker.encode()).hexdigest()
            conn.execute("INSERT INTO stage3_marker VALUES (?,?)", (marker, checksum))
            conn.commit()
            conn.close()
            sqlite3.connect(saas_db).close()
            sqlite3.connect(ops_db).close()
            (art / "synthetic.txt").write_text(marker, encoding="utf-8")
            svc = BackupService(
                backup_root=str(backup),
                side_effect_db=str(se_db),
                saas_db=str(saas_db),
                ops_admin_db=str(ops_db),
                artifact_roots=(str(art),),
            )
            manifest = svc.create_backup()
            corrupt_dir = backup / "corrupt"
            corrupt_dir.mkdir()
            corrupt_rejected = False
            try:
                verify_manifest(str(corrupt_dir))
            except ProductionFoundationError:
                corrupt_rejected = True
            recovery.mkdir()
            restore = RestoreService(target_data_dir=str(recovery))
            backup_dir = str(backup / manifest.backup_id)
            result = restore.restore(backup_dir)
            restored_conn = sqlite3.connect(recovery / "side_effects.sqlite3")
            row = restored_conn.execute("SELECT checksum FROM stage3_marker WHERE id=?", (marker,)).fetchone()
            restored_conn.close()
            restored_art = (recovery / "artifacts" / "synthetic.txt").read_text(encoding="utf-8")
            ok = row and row[0] == checksum and restored_art == marker and corrupt_rejected
            status = GateStatus.PASS if ok else GateStatus.FAIL
            evidence = ReleaseEvidence.begin(gate="3.11_backup_restore", environment="isolated", mode=ExecutionMode.PRODUCTION_LIKE, release_identity=self.config.release_identity)
            evidence.complete(
                status=status,
                classification=VerificationClass.CODE_VERIFIED.value,
                safe_metrics={
                    "backup_id": manifest.backup_id,
                    "restore_ok": bool(row),
                    "corrupt_rejected": corrupt_rejected,
                    "off_host": False,
                },
                operator_action="Configure PANDA_BACKUP_DESTINATION off-host before Stage-4 customer traffic",
            )
            self.store.save(evidence)
            return {"status": status.value, "backup_id": manifest.backup_id, "evidence_id": evidence.evidence_id}

    def run_live(self) -> dict:
        evidence = ReleaseEvidence.begin(gate="3.11_backup_restore_live", environment=self.config.environment, mode=ExecutionMode.LIVE_MUTATING, release_identity=self.config.release_identity)
        evidence.complete(
            status=GateStatus.BLOCKED,
            classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
            operator_action="Operator: run production_foundation.cli backup; restore to isolated target; verify off-host destination",
        )
        self.store.save(evidence)
        return {"status": GateStatus.BLOCKED.value, "evidence_id": evidence.evidence_id}
