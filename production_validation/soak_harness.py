"""Soak/stability harness."""

from __future__ import annotations

import time

from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass


class SoakHarness:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    def run_local(self) -> dict:
        from fastapi.testclient import TestClient
        from tests.test_smoke import load_app

        client = TestClient(load_app().app)
        duration = min(self.config.soak_duration_seconds, 60.0)
        started = time.monotonic()
        end = started + duration
        count = 0
        errors = 0
        mem_start = mem_end = 0
        try:
            import psutil

            mem_start = psutil.Process().memory_info().rss
        except Exception:
            pass
        while time.monotonic() < end:
            resp = client.get("/health")
            count += 1
            if resp.status_code >= 500:
                errors += 1
            time.sleep(0.2)
        try:
            import psutil

            mem_end = psutil.Process().memory_info().rss
        except Exception:
            pass
        actual_duration = time.monotonic() - started
        growth = mem_end - mem_start if mem_start and mem_end else 0
        status = GateStatus.PASS if errors == 0 else GateStatus.FAIL
        evidence = ReleaseEvidence.begin(gate="3.8_soak", environment="local", mode=ExecutionMode.LOCAL_FIXTURE, release_identity=self.config.release_identity)
        evidence.complete(
            status=status,
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics={
                "duration_s": round(actual_duration, 2),
                "requests": count,
                "errors": errors,
                "memory_start_bytes": mem_start,
                "memory_end_bytes": mem_end,
                "memory_growth_bytes": growth,
            },
        )
        self.store.save(evidence)
        return {"status": status.value, "evidence_id": evidence.evidence_id, "metrics": evidence.safe_metrics}
