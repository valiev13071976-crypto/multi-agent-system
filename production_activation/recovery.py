"""Production data and recovery verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RecoveryCheckResult:
    persistent_db: str = "unknown"
    artifacts: str = "unknown"
    workflow: str = "unknown"
    audit: str = "unknown"
    billing_idempotency: str = "unknown"
    backup_freshness: str = "unknown"
    restore_evidence_current: bool = False
    stage3_restore_reusable: bool = False

    def ready(self) -> bool:
        required = (self.persistent_db, self.workflow, self.audit)
        return all(v in {"ready", "verified"} for v in required) and (self.restore_evidence_current or self.stage3_restore_reusable)

    def as_dict(self) -> dict[str, Any]:
        return {
            "persistent_db": self.persistent_db,
            "artifacts": self.artifacts,
            "workflow": self.workflow,
            "audit": self.audit,
            "billing_idempotency": self.billing_idempotency,
            "backup_freshness": self.backup_freshness,
            "restore_evidence_current": self.restore_evidence_current,
            "stage3_restore_reusable": self.stage3_restore_reusable,
            "ready": self.ready(),
        }
