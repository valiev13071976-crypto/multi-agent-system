"""Governed Stage-3 operator live-evidence recorder.

Records explicit operator attestations into the existing EvidenceStore.
Does not auto-pass gates or execute production actions.
"""

from __future__ import annotations

import re
from typing import Any

from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass

OPERATOR_LIVE_GATES = frozenset(
    {
        "3.1_railway_deployment",
        "3.1_restart_persistence",
        "3.2_ai_live",
        "3.5_security_live",
        "3.11_backup_restore_live",
        "3.12_alerts_live",
        "3.15_rollback_live",
    }
)

FORBIDDEN_GATES = frozenset(
    {
        "3.17_release_gate",
        "3.1_env_config",
        "3.2_providers",
        "3.3_smoke",
        "3.5_security",
        "3.6_load",
        "3.7_isolation",
        "3.8_soak",
        "3.9_failure_injection",
        "3.10_crash_recovery",
        "3.11_backup_restore",
    }
)

ACCEPTED_STATUSES = frozenset({"PASS", "FAIL", "BLOCKED"})

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}", re.I),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.I),
    re.compile(r"(api[_-]?key|password|secret|token)\s*[:=]\s*\S+", re.I),
    re.compile(r"Authorization\s*:\s*\S+", re.I),
)


class OperatorEvidenceError(ValueError):
    def __init__(self, code: str, *, message: str = "", details: dict | None = None):
        self.code = str(code)
        self.message = message or code
        self.details = dict(details or {})
        super().__init__(self.message)


def _looks_secret(value: str) -> bool:
    text = str(value or "")
    return any(p.search(text) for p in _SECRET_PATTERNS)


class OperatorEvidenceRecorder:
    """Fail-closed recorder for operator-attested Stage-3 live evidence."""

    def __init__(self, *, config: ValidationConfig | None = None, store: EvidenceStore | None = None):
        self.config = config or ValidationConfig.from_env()
        self.store = store or EvidenceStore()

    def record(
        self,
        *,
        gate: str,
        status: str,
        operator: str,
        note: str,
        confirm_live_verified: bool = False,
        release_identity: str = "",
        artifact_ref: str = "",
        safe_metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gate_name = str(gate or "").strip()
        status_name = str(status or "").strip().upper()
        operator_ref = str(operator or "").strip()
        note_text = str(note or "").strip()
        requested_identity = str(release_identity or "").strip()

        if not gate_name:
            raise OperatorEvidenceError("gate_required")
        if gate_name == "3.17_release_gate" or gate_name in FORBIDDEN_GATES:
            raise OperatorEvidenceError("gate_forbidden", details={"gate": gate_name})
        if gate_name not in OPERATOR_LIVE_GATES:
            raise OperatorEvidenceError("gate_not_allowed", details={"gate": gate_name})
        if status_name not in ACCEPTED_STATUSES:
            raise OperatorEvidenceError("status_unsupported", details={"status": status_name})
        if not operator_ref:
            raise OperatorEvidenceError("operator_required")
        if not note_text:
            raise OperatorEvidenceError("note_required")
        if _looks_secret(operator_ref) or _looks_secret(note_text) or _looks_secret(artifact_ref):
            raise OperatorEvidenceError("secret_like_input_rejected")
        if status_name == "PASS" and not confirm_live_verified:
            raise OperatorEvidenceError("confirm_live_verified_required")

        active_identity = str(self.config.release_identity or "").strip()
        if requested_identity and active_identity and requested_identity != active_identity:
            raise OperatorEvidenceError(
                "release_identity_mismatch",
                details={"expected": active_identity, "got": requested_identity},
            )
        bound_identity = requested_identity or active_identity
        if not bound_identity:
            raise OperatorEvidenceError("release_identity_required")

        if status_name == "PASS":
            classification = VerificationClass.LIVE_VERIFIED.value
            gate_status = GateStatus.PASS
        elif status_name == "FAIL":
            classification = VerificationClass.OPERATOR_ACTION_REQUIRED.value
            gate_status = GateStatus.FAIL
        else:
            classification = VerificationClass.OPERATOR_ACTION_REQUIRED.value
            gate_status = GateStatus.BLOCKED

        metrics = {
            "source": "operator_attestation",
            "operator_ref": operator_ref,
            "note": note_text[:500],
            "gate": gate_name,
            "attested_status": status_name,
        }
        if safe_metrics:
            for key, value in dict(safe_metrics).items():
                key_s = str(key)
                val_s = str(value)
                if _looks_secret(key_s) or _looks_secret(val_s):
                    raise OperatorEvidenceError("secret_like_input_rejected")
                metrics[key_s] = value

        evidence = ReleaseEvidence.begin(
            gate=gate_name,
            environment=self.config.environment,
            mode=ExecutionMode.LIVE_SAFE,
            classification=classification,
            release_identity=bound_identity,
        )
        evidence.complete(
            status=gate_status,
            classification=classification,
            safe_metrics=metrics,
            artifact_ref=str(artifact_ref or "")[:256],
            operator_action=f"operator_attestation:{operator_ref}",
        )

        previous = self.store.latest_for_gate(gate_name)
        self.store.save(evidence)
        if previous and previous.get("evidence_id") and previous.get("evidence_id") != evidence.evidence_id:
            self.store.supersede(previous["evidence_id"], evidence.evidence_id)

        return {
            "status": evidence.status,
            "gate": evidence.gate,
            "classification": evidence.classification,
            "evidence_id": evidence.evidence_id,
            "environment": evidence.environment,
            "release_identity": evidence.release_identity,
            "operator_ref": operator_ref,
        }
