"""Failure injection harness — scoped, reversible, synthetic."""

from __future__ import annotations

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.retry import RetryPolicy, execute_with_retry
from production_validation.config import ValidationConfig
from production_validation.evidence_store import EvidenceStore
from production_validation.models import ExecutionMode, GateStatus, ReleaseEvidence, VerificationClass
from providers.governor import GovernorLimits, InMemoryProviderGovernorStore, ProviderGovernor


class FailureInjectionHarness:
    def __init__(self, *, config: ValidationConfig, store: EvidenceStore | None = None):
        self.config = config
        self.store = store or EvidenceStore()

    def run(self) -> dict:
        results: dict[str, str] = {}
        attempts = {"n": 0}

        def flaky_429():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ProductionProviderError(
                    ProviderErrorCategory.RATE_LIMITED,
                    retryable=True,
                    provider_id="test",
                    retry_after_seconds=0.01,
                )
            return "ok"

        try:
            execute_with_retry(flaky_429, policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01))
            results["429_retry"] = "PASS"
        except Exception:
            results["429_retry"] = "FAIL"

        auth_attempts = {"n": 0}

        def auth_fail():
            auth_attempts["n"] += 1
            raise ProductionProviderError(ProviderErrorCategory.AUTHENTICATION_FAILED, retryable=False, provider_id="test")

        try:
            execute_with_retry(auth_fail, policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.01))
            results["auth_no_storm"] = "FAIL"
        except ProductionProviderError:
            results["auth_no_storm"] = "PASS" if auth_attempts["n"] == 1 else "FAIL"

        store = InMemoryProviderGovernorStore(GovernorLimits(failure_threshold=2, cooldown_seconds=1))
        gov = ProviderGovernor(store=store, limits=GovernorLimits(failure_threshold=2, cooldown_seconds=1))
        for _ in range(3):
            gov.record_failure("openai", "")
        state = gov.breaker_state("openai", "")
        results["circuit"] = "PASS" if str(state).upper() in {"OPEN", "COOLDOWN", "DEGRADED", "HALF_OPEN"} else "FAIL"

        overall = GateStatus.PASS if all(v == "PASS" for v in results.values()) else GateStatus.FAIL
        evidence = ReleaseEvidence.begin(gate="3.9_failure_injection", environment="local", mode=ExecutionMode.LOCAL_FIXTURE, release_identity=self.config.release_identity)
        evidence.complete(status=overall, classification=VerificationClass.CODE_VERIFIED.value, safe_metrics={"cases": results, "breaker_state": state})
        self.store.save(evidence)
        return {"status": overall.value, "cases": results, "evidence_id": evidence.evidence_id}
