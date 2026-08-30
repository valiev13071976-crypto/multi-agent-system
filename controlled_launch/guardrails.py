"""Guardrail evaluation for controlled launch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GuardrailObservation:
    name: str
    baseline: float | None
    threshold: float | None
    window: str
    observed: float | None
    result: str
    action: str = "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "baseline": self.baseline,
            "threshold": self.threshold,
            "window": self.window,
            "observed": self.observed,
            "result": self.result,
            "action": self.action,
        }


@dataclass
class GuardrailEvaluator:
    observations: list[GuardrailObservation] = field(default_factory=list)

    def evaluate(
        self,
        *,
        metrics: dict[str, float],
        guardrails: dict[str, Any],
        security_events: list[str] | None = None,
    ) -> tuple[str, list[GuardrailObservation]]:
        security_events = security_events or []
        p0_events = {
            "cross_tenant_exposure",
            "auth_bypass",
            "secret_exposure",
            "billing_forgery",
            "unauthorized_side_effect",
        }
        for event in security_events:
            if event in p0_events:
                obs = GuardrailObservation(
                    name="security_zero_tolerance",
                    baseline=None,
                    threshold=None,
                    window="instant",
                    observed=None,
                    result="FAIL",
                    action="ABORT",
                )
                self.observations.append(obs)
                return "ABORT", self.observations
        action = "none"
        for name, spec in guardrails.items():
            if not isinstance(spec, dict):
                continue
            threshold = spec.get("threshold")
            baseline = spec.get("baseline")
            observed = metrics.get(name)
            result = "PASS"
            local_action = "none"
            if observed is not None and threshold is not None and observed > threshold:
                result = "FAIL"
                local_action = str(spec.get("action") or "HOLD")
            obs = GuardrailObservation(
                name=name,
                baseline=float(baseline) if baseline is not None else None,
                threshold=float(threshold) if threshold is not None else None,
                window=str(spec.get("window") or "observation"),
                observed=float(observed) if observed is not None else None,
                result=result,
                action=local_action,
            )
            self.observations.append(obs)
            if result == "FAIL" and local_action in {"HOLD", "ABORT"}:
                action = local_action if action == "none" else ("ABORT" if "ABORT" in {action, local_action} else action)
        return action, self.observations
