"""Production smoke test harness."""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass


class SmokeRunner:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    def run_local(self, app) -> dict[str, Any]:
        from fastapi.testclient import TestClient

        client = TestClient(app)
        started = time.monotonic()
        results: dict[str, str] = {}
        cases = [
            ("health", lambda: client.get("/health")),
            ("readiness", lambda: client.get("/ready")),
        ]
        for name, fn in cases:
            resp = fn()
            if name == "readiness" and resp.status_code not in {200, 503}:
                results[name] = f"FAIL:{resp.status_code}"
            elif name == "health":
                results[name] = "PASS" if resp.status_code == 200 else f"FAIL:{resp.status_code}"
            else:
                results[name] = "PASS" if resp.status_code == 200 else f"DEGRADED:{resp.status_code}"
        body = client.get("/health").json()
        if any(k in str(body).lower() for k in ("api_key", "secret", "token", "password")):
            results["secret_leakage"] = "FAIL"
        else:
            results["secret_leakage"] = "PASS"
        duration = round(time.monotonic() - started, 3)
        overall = GateStatus.PASS if all(v.startswith("PASS") or v.startswith("DEGRADED") for k, v in results.items() if k != "secret_leakage") and results.get("secret_leakage") == "PASS" else GateStatus.FAIL
        evidence = ReleaseEvidence.begin(gate="3.3_smoke", environment="local", mode=ExecutionMode.LOCAL_FIXTURE, release_identity=self.config.release_identity)
        evidence.complete(status=overall, classification=VerificationClass.CODE_VERIFIED.value, safe_metrics={"duration_s": duration, "cases": results})
        self.store.save(evidence)
        return {"status": overall.value, "cases": results, "duration_s": duration, "evidence_id": evidence.evidence_id}

    def run_live(self) -> dict[str, Any]:
        url = self.config.production_url
        if not url:
            evidence = ReleaseEvidence.begin(gate="3.3_smoke", environment=self.config.environment, mode=ExecutionMode.LIVE_SAFE, release_identity=self.config.release_identity)
            evidence.complete(
                status=GateStatus.BLOCKED,
                classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
                operator_action="Set PRODUCTION_VALIDATION_URL or PUBLIC_URL to deployed production URL",
            )
            self.store.save(evidence)
            return {"status": GateStatus.BLOCKED.value, "operator_action": evidence.operator_action, "evidence_id": evidence.evidence_id}
        started = time.monotonic()
        results: dict[str, str] = {}
        timeout = httpx.Timeout(10.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for path, name in (("/health", "health"), ("/ready", "readiness")):
                try:
                    resp = client.get(f"{url}{path}")
                    results[name] = "PASS" if resp.status_code == 200 else f"HTTP_{resp.status_code}"
                    if name == "health" and "secret" in resp.text.lower():
                        results["secret_leakage"] = "FAIL"
                except httpx.RequestError as exc:
                    results[name] = f"ERROR:{type(exc).__name__}"
            results.setdefault("secret_leakage", "PASS")
        duration = round(time.monotonic() - started, 3)
        overall = GateStatus.PASS if results.get("health") == "PASS" else GateStatus.FAIL
        evidence = ReleaseEvidence.begin(gate="3.3_smoke", environment=self.config.environment, mode=ExecutionMode.LIVE_SAFE, release_identity=self.config.release_identity)
        evidence.complete(
            status=overall,
            classification=VerificationClass.LIVE_VERIFIED.value if overall == GateStatus.PASS else VerificationClass.OPERATOR_ACTION_REQUIRED.value,
            safe_metrics={"duration_s": duration, "url": url, "cases": results},
        )
        self.store.save(evidence)
        return {"status": overall.value, "cases": results, "duration_s": duration, "evidence_id": evidence.evidence_id}
