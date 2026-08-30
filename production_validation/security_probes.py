"""Production security probes."""

from __future__ import annotations

from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass


class SecurityProbeHarness:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    def run_local(self) -> dict:
        from fastapi.testclient import TestClient
        from tests.test_smoke import load_app

        main_mod = load_app(SECURITY_AUTH_MODE="required", PANDA_API_KEYS="k|t|u|user|test-key-value-only")
        client = TestClient(main_mod.app)
        results: dict[str, str] = {}
        unauth = client.get("/api/admin/ops/health")
        results["unauthenticated_denied"] = "PASS" if unauth.status_code in {401, 403} else f"FAIL:{unauth.status_code}"
        health = client.get("/health").text.lower()
        results["health_no_secret"] = "PASS" if "test-key-value-only" not in health and "api_key" not in health else "FAIL"
        forged = client.post(
            "/integrations/billing/stripe/webhook",
            content=b"{}",
            headers={"Stripe-Signature": "t=1,v1=bad"},
        )
        results["billing_webhook_forgery"] = "PASS" if forged.status_code in {401, 403, 503} else f"FAIL:{forged.status_code}"
        tg = client.post("/integrations/telegram/webhook/tenant-a", content=b"{}", headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
        results["telegram_webhook_forgery"] = "PASS" if tg.status_code in {401, 403, 503} else f"FAIL:{tg.status_code}"
        overall = GateStatus.PASS if all(v == "PASS" for v in results.values()) else GateStatus.FAIL
        evidence = ReleaseEvidence.begin(gate="3.5_security", environment="local", mode=ExecutionMode.LOCAL_FIXTURE, release_identity=self.config.release_identity)
        evidence.complete(status=overall, classification=VerificationClass.CODE_VERIFIED.value, safe_metrics={"cases": results})
        self.store.save(evidence)
        return {"status": overall.value, "cases": results, "evidence_id": evidence.evidence_id}

    def run_live(self) -> dict:
        evidence = ReleaseEvidence.begin(gate="3.5_security_live", environment=self.config.environment, mode=ExecutionMode.LIVE_SAFE, release_identity=self.config.release_identity)
        if not self.config.production_url:
            evidence.complete(status=GateStatus.BLOCKED, classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value, operator_action="Run production security probes against deployed URL")
        else:
            evidence.complete(status=GateStatus.BLOCKED, classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value, operator_action="Operator: execute production security matrix against live URL")
        self.store.save(evidence)
        return {"status": GateStatus.BLOCKED.value, "evidence_id": evidence.evidence_id}
