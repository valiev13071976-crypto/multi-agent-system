"""Shadow traffic execution and evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from controlled_launch.models import LaunchEvidence, ShadowGateResult, VerificationClass
from controlled_launch.side_effect_policy import ShadowSideEffectPolicy
from evals.shadow import ShadowEvidence, ShadowRunner


@dataclass
class ShadowMetrics:
    requests: int = 0
    failures: int = 0
    skipped_capacity: int = 0
    total_cost: float = 0.0
    active: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "skipped_capacity": self.skipped_capacity,
            "total_cost": self.total_cost,
            "active": self.active,
        }


@dataclass
class ShadowController:
    runner: ShadowRunner = field(default_factory=ShadowRunner)
    metrics: ShadowMetrics = field(default_factory=ShadowMetrics)
    max_concurrency: int = 10
    max_requests: int = 1000
    max_cost: float = 100.0

    def can_execute(self) -> bool:
        if self.metrics.requests >= self.max_requests:
            return False
        if self.metrics.total_cost >= self.max_cost:
            return False
        if self.metrics.active >= self.max_concurrency:
            return False
        return True

    def execute(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        input_ref: str,
        scorer: Callable | None = None,
        side_effect_type: str = "",
    ) -> tuple[ShadowEvidence | None, str]:
        if side_effect_type:
            try:
                ShadowSideEffectPolicy.authorize(
                    mode="SHADOW",
                    side_effect_type=side_effect_type,
                    candidate_target=True,
                    shadow_path=True,
                )
            except Exception:
                self.metrics.failures += 1
                return None, "SHADOW_SIDE_EFFECT_DENIED"
        if not self.can_execute():
            self.metrics.skipped_capacity += 1
            return None, "SHADOW_SKIPPED_CAPACITY"
        self.metrics.active += 1
        try:
            evidence = self.runner.run(
                candidate_id,
                input_ref,
                tenant_id=tenant_id,
                scorer=scorer,
            )
            self.metrics.requests += 1
            return evidence, "recorded"
        except Exception:
            self.metrics.failures += 1
            return None, "shadow_error"
        finally:
            self.metrics.active = max(0, self.metrics.active - 1)

    def evaluate_gate(
        self,
        *,
        candidate_id: str,
        environment: str,
        policy_version: str,
        guardrails: dict[str, Any] | None = None,
        security_events: list[str] | None = None,
    ) -> tuple[ShadowGateResult, LaunchEvidence]:
        guardrails = guardrails or {}
        security_events = security_events or []
        zero_tolerance = {
            "cross_tenant_exposure",
            "auth_bypass",
            "secret_exposure",
            "real_shadow_side_effect",
            "billing_mutation",
            "unsafe_data_scope",
        }
        for event in security_events:
            if event in zero_tolerance:
                ev = LaunchEvidence.create(
                    candidate_id=candidate_id,
                    environment=environment,
                    policy_version=policy_version,
                    gate="4.5_shadow_gate",
                    status=ShadowGateResult.SHADOW_FAIL.value,
                    classification=VerificationClass.CODE_VERIFIED.value,
                    safe_metrics={"security_event": event, "metrics": self.metrics.as_dict()},
                )
                return ShadowGateResult.SHADOW_FAIL, ev
        max_error_rate = float(guardrails.get("max_error_rate") or 0.25)
        error_rate = self.metrics.failures / max(1, self.metrics.requests)
        if error_rate > max_error_rate:
            result = ShadowGateResult.SHADOW_HOLD
        elif self.metrics.requests == 0:
            result = ShadowGateResult.SHADOW_HOLD
        else:
            result = ShadowGateResult.SHADOW_PASS
        ev = LaunchEvidence.create(
            candidate_id=candidate_id,
            environment=environment,
            policy_version=policy_version,
            gate="4.5_shadow_gate",
            status=result.value,
            classification=VerificationClass.CODE_VERIFIED.value,
            safe_metrics={"error_rate": error_rate, "metrics": self.metrics.as_dict()},
        )
        return result, ev
