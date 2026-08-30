"""Live provider verification harness."""

from __future__ import annotations

import os
from typing import Any

from production_validation.config import LAUNCH_PROVIDER_MATRIX, ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass
from production_foundation.config import reject_placeholder_secret


class LiveProviderValidator:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None, env: dict | None = None):
        self.config = config
        self.store = store or EvidenceStore()
        self.env = env if env is not None else dict(os.environ)

    def _openai_live_verified(self) -> bool:
        latest = self.store.latest_for_gate("3.2_ai_live")
        if not latest:
            return False
        return (
            latest.get("status") == GateStatus.PASS.value
            and latest.get("classification") == VerificationClass.LIVE_VERIFIED.value
        )

    def build_matrix(self) -> list[dict[str, Any]]:
        rows = []
        openai_live = self._openai_live_verified()
        for spec in LAUNCH_PROVIDER_MATRIX:
            configured = any(str(self.env.get(k) or "").strip() and reject_placeholder_secret(str(self.env.get(k) or "")) for k in spec.env_keys if self.env.get(k))
            enabled = configured or spec.requirement == "NOT_USED_AT_LAUNCH"
            if spec.requirement == "NOT_USED_AT_LAUNCH":
                verification = VerificationClass.NOT_ENABLED.value
                status = GateStatus.SKIP
            elif not configured:
                verification = VerificationClass.OPERATOR_ACTION_REQUIRED.value
                status = GateStatus.BLOCKED if spec.requirement == "REQUIRED_FOR_STAGE4" else GateStatus.SKIP
            elif spec.provider_id == "openai" and openai_live:
                verification = VerificationClass.LIVE_VERIFIED.value
                status = GateStatus.PASS
            else:
                verification = VerificationClass.CONFIG_VERIFIED.value
                status = GateStatus.BLOCKED
            rows.append(
                {
                    "provider": spec.provider_id,
                    "requirement": spec.requirement,
                    "enabled": enabled,
                    "configured": configured,
                    "verification": verification,
                    "status": status.value,
                    "operator_action": "" if configured else f"Configure {','.join(spec.env_keys)}",
                }
            )
        return rows

    def verify_required_ai_live(self) -> dict[str, Any]:
        existing = self.store.latest_for_gate("3.2_ai_live")
        if (
            existing
            and existing.get("status") == GateStatus.PASS.value
            and existing.get("classification") == VerificationClass.LIVE_VERIFIED.value
        ):
            return {"status": GateStatus.PASS.value, "evidence_id": existing.get("evidence_id", "")}
        evidence = ReleaseEvidence.begin(gate="3.2_ai_live", environment=self.config.environment, mode=ExecutionMode.LIVE_SAFE, release_identity=self.config.release_identity)
        key = str(self.env.get("OPENAI_API_KEY") or "").strip()
        url = self.config.production_url
        if not key or not reject_placeholder_secret(key):
            evidence.complete(
                status=GateStatus.BLOCKED,
                classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
                operator_action="Set OPENAI_API_KEY in production environment and run bounded live AI smoke",
            )
            self.store.save(evidence)
            return {"status": GateStatus.BLOCKED.value, "evidence_id": evidence.evidence_id}
        if not url:
            evidence.complete(
                status=GateStatus.BLOCKED,
                classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
                operator_action="Live AI verification requires deployed production URL + provider key",
            )
            self.store.save(evidence)
            return {"status": GateStatus.BLOCKED.value, "evidence_id": evidence.evidence_id}
        evidence.complete(
            status=GateStatus.BLOCKED,
            classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
            operator_action="Operator: run bounded live AI request against production deployment with OPENAI_API_KEY configured",
            safe_metrics={"configured": True, "live_call": False},
        )
        self.store.save(evidence)
        return {"status": GateStatus.BLOCKED.value, "evidence_id": evidence.evidence_id}

    def run_gate(self) -> dict[str, Any]:
        matrix = self.build_matrix()
        required_blocked = [r for r in matrix if r["requirement"] == "REQUIRED_FOR_STAGE4" and r["verification"] != VerificationClass.LIVE_VERIFIED.value]
        status = GateStatus.BLOCKED if required_blocked else GateStatus.PASS
        evidence = ReleaseEvidence.begin(gate="3.2_providers", environment=self.config.environment, mode=ExecutionMode.LIVE_SAFE, release_identity=self.config.release_identity)
        evidence.complete(
            status=status,
            classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value if status == GateStatus.BLOCKED else VerificationClass.LIVE_VERIFIED.value,
            safe_metrics={"matrix_count": len(matrix), "required_blocked": len(required_blocked)},
            operator_action="Configure and live-verify REQUIRED_FOR_STAGE4 providers" if required_blocked else "",
        )
        self.store.save(evidence)
        return {"status": status.value, "matrix": matrix, "evidence_id": evidence.evidence_id}
