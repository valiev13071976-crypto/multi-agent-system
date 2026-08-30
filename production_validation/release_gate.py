"""Release readiness gate evaluator."""

from __future__ import annotations

from dataclasses import dataclass, field

from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import GateStatus, ReleaseEvidence, VerificationClass


MANDATORY_LIVE_GATES = (
    "3.1_railway_deployment",
    "3.1_restart_persistence",
    "3.2_ai_live",
    "3.3_smoke",
    "3.5_security_live",
    "3.11_backup_restore_live",
    "3.12_alerts_live",
    "3.15_rollback_live",
)


@dataclass
class ReleaseGateResult:
    verdict: str
    engineering: str
    live_validation: str
    release_readiness: str
    gates: dict[str, str] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)
    evidence_id: str = ""


class ReleaseGateEvaluator:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    def evaluate(self, local_results: dict[str, str] | None = None) -> ReleaseGateResult:
        local_results = local_results or {}
        evidence_items = self.store.all_completed()
        # Deterministic current status: latest completed evidence per gate
        # (not file-id iteration order, which can mis-order PASS vs BLOCKED).
        gate_names = {item["gate"] for item in evidence_items}
        gate_status: dict[str, str] = {}
        for gate_name in gate_names:
            latest = self.store.latest_for_gate(gate_name)
            if latest and latest.get("status"):
                gate_status[gate_name] = str(latest["status"])
        gate_status.update(local_results)
        blocked = [g for g in MANDATORY_LIVE_GATES if gate_status.get(g) != GateStatus.PASS.value]
        local_pass = all(v == GateStatus.PASS.value for k, v in local_results.items() if k.startswith("3.") and "live" not in k)
        engineering = "PASS" if local_pass else "FAIL"
        live = "PASS" if not blocked and self.config.production_url else "BLOCKED"
        readiness = "READY" if engineering == "PASS" and live == "PASS" else "NOT_READY"
        verdict = "PRODUCTION_VALIDATION_PASS" if readiness == "READY" else "PRODUCTION_VALIDATION_BLOCKED"
        evidence = ReleaseEvidence.begin(gate="3.17_release_gate", environment=self.config.environment, mode="READ_ONLY", release_identity=self.config.release_identity)
        evidence.complete(
            status=GateStatus.PASS if verdict == "PRODUCTION_VALIDATION_PASS" else GateStatus.BLOCKED,
            classification=VerificationClass.NOT_APPLICABLE.value,
            safe_metrics={"verdict": verdict, "engineering": engineering, "live": live, "readiness": readiness, "blocked_gates": blocked},
        )
        self.store.save(evidence)
        return ReleaseGateResult(
            verdict=verdict,
            engineering=engineering,
            live_validation=live,
            release_readiness=readiness,
            gates=gate_status,
            blocked=blocked,
            evidence_id=evidence.evidence_id,
        )
