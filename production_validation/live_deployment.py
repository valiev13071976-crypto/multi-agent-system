"""Live deployment validation harness."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass
from production_foundation.config import validate_production_config


class DeploymentValidator:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    def validate_config(self, env: dict | None = None) -> dict[str, Any]:
        source = env if env is not None else dict(os.environ)
        report = validate_production_config(source)
        from saas_product.deployment import validate_production_config as commercial

        commercial_report = commercial(env=source)
        keys_present = {
            "PANDA_ENV": bool(str(source.get("PANDA_ENV") or "").strip()),
            "PANDA_DATA_DIR": bool(str(source.get("PANDA_DATA_DIR") or "").strip()),
            "PUBLIC_URL": bool(str(source.get("PUBLIC_URL") or "").strip()),
            "SECURITY_AUTH_MODE": str(source.get("SECURITY_AUTH_MODE") or ""),
            "SIDE_EFFECT_PERSISTENCE_BACKEND": str(source.get("SIDE_EFFECT_PERSISTENCE_BACKEND") or ""),
        }
        status = GateStatus.PASS if report.overall in {"PASS", "WARN"} else GateStatus.FAIL
        evidence = ReleaseEvidence.begin(gate="3.1_env_config", environment=self.config.environment, mode=ExecutionMode.PRODUCTION_LIKE, release_identity=self.config.release_identity)
        evidence.complete(
            status=status,
            classification=VerificationClass.CONFIG_VERIFIED.value if status == GateStatus.PASS else VerificationClass.OPERATOR_ACTION_REQUIRED.value,
            safe_metrics={"foundation_overall": report.overall, "commercial_overall": commercial_report.overall, "keys_present": keys_present},
        )
        self.store.save(evidence)
        return {"status": status.value, "evidence_id": evidence.evidence_id, "keys_present": keys_present}

    def validate_railway_live(self) -> dict[str, Any]:
        url = self.config.production_url
        evidence = ReleaseEvidence.begin(gate="3.1_railway_deployment", environment=self.config.environment, mode=ExecutionMode.LIVE_SAFE, release_identity=self.config.release_identity)
        if not url:
            evidence.complete(
                status=GateStatus.BLOCKED,
                classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
                operator_action="Deploy to Railway; set PUBLIC_URL; grant Cursor/operator Railway access for live verification",
            )
            self.store.save(evidence)
            return {"status": GateStatus.BLOCKED.value, "evidence_id": evidence.evidence_id, "operator_action": evidence.operator_action}
        try:
            import httpx

            resp = httpx.get(f"{url}/health", timeout=10.0, follow_redirects=True)
            tls_ok = url.startswith("https://")
            metrics = {"http_status": resp.status_code, "tls": tls_ok, "url": url}
            status = GateStatus.PASS if resp.status_code == 200 and tls_ok else GateStatus.FAIL
            evidence.complete(status=status, classification=VerificationClass.LIVE_VERIFIED.value if status == GateStatus.PASS else VerificationClass.OPERATOR_ACTION_REQUIRED.value, safe_metrics=metrics)
        except Exception as exc:
            evidence.complete(status=GateStatus.FAIL, classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value, failure_category=type(exc).__name__)
        self.store.save(evidence)
        return {"status": evidence.status, "evidence_id": evidence.evidence_id, "safe_metrics": evidence.safe_metrics}

    def validate_restart_persistence_live(self) -> dict[str, Any]:
        evidence = ReleaseEvidence.begin(gate="3.1_restart_persistence", environment=self.config.environment, mode=ExecutionMode.LIVE_MUTATING, release_identity=self.config.release_identity)
        evidence.complete(
            status=GateStatus.BLOCKED,
            classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
            operator_action="Operator: create synthetic durable state, trigger Railway restart/redeploy, verify persistence",
        )
        self.store.save(evidence)
        return {"status": GateStatus.BLOCKED.value, "evidence_id": evidence.evidence_id}
