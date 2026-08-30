"""Production activation observability."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ActivationMetrics:
    activation_attempts_total: int = 0
    activation_success_total: int = 0
    activation_failure_total: int = 0
    rollback_total: int = 0
    smoke_fail_total: int = 0
    hypercare_p0_total: int = 0

    def inc(self, name: str, value: int = 1) -> None:
        current = getattr(self, name, 0)
        setattr(self, name, current + value)

    def as_dict(self) -> dict:
        return {
            "activation_attempts_total": self.activation_attempts_total,
            "activation_success_total": self.activation_success_total,
            "activation_failure_total": self.activation_failure_total,
            "rollback_total": self.rollback_total,
            "smoke_fail_total": self.smoke_fail_total,
            "hypercare_p0_total": self.hypercare_p0_total,
        }
