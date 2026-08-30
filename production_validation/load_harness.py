"""Bounded load test harness."""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any

import httpx

from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass


class LoadHarness:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    async def _worker(self, client: httpx.AsyncClient, url: str, latencies: list[float], errors: list[str], stop_at: float, counter: dict) -> None:
        while time.monotonic() < stop_at and counter["n"] < self.config.max_load_requests:
            started = time.monotonic()
            try:
                resp = await client.get(url)
                latencies.append((time.monotonic() - started) * 1000)
                if resp.status_code >= 500:
                    errors.append(f"http_{resp.status_code}")
            except Exception as exc:
                errors.append(type(exc).__name__)
            counter["n"] += 1
            await asyncio.sleep(0.01)

    def run_local(self, base_url: str = "http://testserver") -> dict[str, Any]:
        from fastapi.testclient import TestClient
        from tests.test_smoke import load_app

        app = load_app().app
        client = TestClient(app)
        started = time.monotonic()
        latencies: list[float] = []
        errors = 0
        count = min(self.config.max_load_requests, 100)
        for _ in range(count):
            t0 = time.monotonic()
            resp = client.get("/health")
            latencies.append((time.monotonic() - t0) * 1000)
            if resp.status_code >= 500:
                errors += 1
        duration = time.monotonic() - started
        metrics = self._metrics(latencies, errors, count, duration)
        status = GateStatus.PASS if errors == 0 else GateStatus.FAIL
        evidence = ReleaseEvidence.begin(gate="3.6_load", environment="local", mode=ExecutionMode.LOCAL_FIXTURE, release_identity=self.config.release_identity)
        evidence.complete(status=status, classification=VerificationClass.CODE_VERIFIED.value, safe_metrics=metrics)
        self.store.save(evidence)
        return {"status": status.value, "metrics": metrics, "evidence_id": evidence.evidence_id}

    def run_live(self) -> dict[str, Any]:
        url = self.config.production_url
        evidence = ReleaseEvidence.begin(gate="3.6_load", environment=self.config.environment, mode=ExecutionMode.LIVE_SAFE, release_identity=self.config.release_identity)
        if not url:
            evidence.complete(status=GateStatus.BLOCKED, classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value, operator_action="Set production URL for live load validation")
            self.store.save(evidence)
            return {"status": GateStatus.BLOCKED.value, "evidence_id": evidence.evidence_id}
        evidence.complete(status=GateStatus.BLOCKED, classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value, operator_action="Operator: run bounded load against production with STAGE3_LOAD_* limits")
        self.store.save(evidence)
        return {"status": GateStatus.BLOCKED.value, "evidence_id": evidence.evidence_id}

    def _metrics(self, latencies: list[float], errors: int, count: int, duration: float) -> dict[str, Any]:
        latencies_sorted = sorted(latencies) if latencies else [0.0]
        p50 = statistics.median(latencies_sorted)
        p95 = latencies_sorted[int(min(len(latencies_sorted) - 1, len(latencies_sorted) * 0.95))] if latencies_sorted else 0.0
        p99 = latencies_sorted[int(min(len(latencies_sorted) - 1, len(latencies_sorted) * 0.99))] if latencies_sorted else 0.0
        return {
            "requests": count,
            "errors": errors,
            "error_rate": round(errors / max(count, 1), 4),
            "duration_s": round(duration, 3),
            "throughput_rps": round(count / max(duration, 0.001), 2),
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "p99_ms": round(p99, 2),
        }
