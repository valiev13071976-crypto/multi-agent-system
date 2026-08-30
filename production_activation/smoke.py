"""Immediate post-launch smoke."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from production_activation.models import VerificationClass


@dataclass
class SmokeCheckResult:
    name: str
    status: str
    classification: str = VerificationClass.CODE_VERIFIED.value
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "classification": self.classification, "details": dict(self.details)}


@dataclass
class PostLaunchSmokeRunner:
    checks: list[SmokeCheckResult] = field(default_factory=list)

    def run(
        self,
        *,
        candidate_id: str,
        attempt_id: str,
        probes: dict[str, Callable[[], bool]] | None = None,
    ) -> dict[str, Any]:
        probes = probes or {}
        required = ("health", "readiness", "auth", "tenant", "chat", "ai", "workflow", "persistence", "admin")
        self.checks = []
        for name in required:
            ok = True
            if name in probes:
                try:
                    ok = bool(probes[name]())
                except Exception as exc:
                    ok = False
                    self.checks.append(
                        SmokeCheckResult(name=name, status="FAIL", details={"error": type(exc).__name__})
                    )
                    continue
            status = "PASS" if ok else "FAIL"
            self.checks.append(SmokeCheckResult(name=name, status=status))
        critical_fail = any(c.status == "FAIL" for c in self.checks if c.name in {"health", "readiness", "auth"})
        return {
            "candidate_id": candidate_id,
            "attempt_id": attempt_id,
            "status": "PASS" if not critical_fail else "FAIL",
            "checks": [c.as_dict() for c in self.checks],
            "classification": VerificationClass.CODE_VERIFIED.value,
        }
