"""Immediate post-launch smoke.

Engineering mode may treat absent probes as CODE_VERIFIED PASS (unit/fixture path).
Live/production mode requires explicit observed results for every mandatory check;
missing probes fail closed and NEVER become LIVE_VERIFIED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from production_activation.models import VerificationClass


REQUIRED_CHECKS = ("health", "readiness", "auth", "tenant", "chat", "ai", "workflow", "persistence", "admin")
CRITICAL_CHECKS = frozenset({"health", "readiness", "auth"})
SMOKE_MODE_ENGINEERING = "engineering"
SMOKE_MODE_LIVE = "live"


@dataclass
class SmokeCheckResult:
    name: str
    status: str
    classification: str = VerificationClass.CODE_VERIFIED.value
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "classification": self.classification, "details": dict(self.details)}


def _normalize_observed(value: Any) -> tuple[bool | None, str]:
    """Return (ok, status_token). ok=None means missing/invalid."""
    if value is None:
        return None, "MISSING"
    if isinstance(value, bool):
        return value, "PASS" if value else "FAIL"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return None, "INVALID"
    text = str(value).strip().upper()
    if text in {"PASS", "OK", "TRUE", "1"}:
        return True, "PASS"
    if text in {"FAIL", "FALSE", "0", "ERROR"}:
        return False, "FAIL"
    if text in {"MISSING", "", "NONE", "NULL"}:
        return None, "MISSING"
    return None, "INVALID"


@dataclass
class PostLaunchSmokeRunner:
    checks: list[SmokeCheckResult] = field(default_factory=list)

    def run(
        self,
        *,
        candidate_id: str,
        attempt_id: str,
        probes: dict[str, Callable[[], bool]] | None = None,
        mode: str = SMOKE_MODE_ENGINEERING,
        observed: dict[str, Any] | None = None,
        plan_id: str = "",
        release_identity: str = "",
    ) -> dict[str, Any]:
        probes = probes or {}
        observed = observed or {}
        live = mode == SMOKE_MODE_LIVE
        classification = VerificationClass.LIVE_VERIFIED.value if live else VerificationClass.CODE_VERIFIED.value
        self.checks = []
        missing: list[str] = []
        invalid: list[str] = []

        for name in REQUIRED_CHECKS:
            if live:
                if name not in observed and name not in probes:
                    missing.append(name)
                    self.checks.append(
                        SmokeCheckResult(
                            name=name,
                            status="FAIL",
                            classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
                            details={"reason": "missing_live_probe"},
                        )
                    )
                    continue
                if name in observed:
                    ok, token = _normalize_observed(observed[name])
                    if ok is None:
                        if token == "MISSING":
                            missing.append(name)
                        else:
                            invalid.append(name)
                        self.checks.append(
                            SmokeCheckResult(
                                name=name,
                                status="FAIL",
                                classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
                                details={"reason": f"observed_{token.lower()}"},
                            )
                        )
                        continue
                    status = "PASS" if ok else "FAIL"
                    self.checks.append(
                        SmokeCheckResult(
                            name=name,
                            status=status,
                            classification=classification if status == "PASS" else VerificationClass.OPERATOR_ACTION_REQUIRED.value,
                        )
                    )
                    continue
                # callable probe provided for live path
                try:
                    ok = bool(probes[name]())
                except Exception as exc:
                    self.checks.append(
                        SmokeCheckResult(
                            name=name,
                            status="FAIL",
                            classification=VerificationClass.OPERATOR_ACTION_REQUIRED.value,
                            details={"error": type(exc).__name__},
                        )
                    )
                    continue
                status = "PASS" if ok else "FAIL"
                self.checks.append(
                    SmokeCheckResult(
                        name=name,
                        status=status,
                        classification=classification if status == "PASS" else VerificationClass.OPERATOR_ACTION_REQUIRED.value,
                    )
                )
                continue

            # engineering mode — preserve historical default (absent probe => PASS / CODE_VERIFIED)
            ok = True
            if name in probes:
                try:
                    ok = bool(probes[name]())
                except Exception as exc:
                    self.checks.append(
                        SmokeCheckResult(name=name, status="FAIL", details={"error": type(exc).__name__})
                    )
                    continue
            elif name in observed:
                ok_obs, token = _normalize_observed(observed[name])
                if ok_obs is None:
                    ok = False
                else:
                    ok = ok_obs
            status = "PASS" if ok else "FAIL"
            self.checks.append(SmokeCheckResult(name=name, status=status, classification=VerificationClass.CODE_VERIFIED.value))

        critical_fail = any(c.status == "FAIL" for c in self.checks if c.name in CRITICAL_CHECKS)
        any_fail = any(c.status == "FAIL" for c in self.checks)
        if live:
            overall_pass = not any_fail and not missing and not invalid
            result_classification = (
                VerificationClass.LIVE_VERIFIED.value
                if overall_pass
                else VerificationClass.OPERATOR_ACTION_REQUIRED.value
            )
            status = "PASS" if overall_pass else "FAIL"
        else:
            overall_pass = not critical_fail
            result_classification = VerificationClass.CODE_VERIFIED.value
            status = "PASS" if overall_pass else "FAIL"

        return {
            "candidate_id": candidate_id,
            "plan_id": plan_id,
            "release_identity": release_identity,
            "attempt_id": attempt_id,
            "status": status,
            "checks": [c.as_dict() for c in self.checks],
            "classification": result_classification,
            "mode": mode,
            "missing_probes": missing,
            "invalid_probes": invalid,
            "critical_failed": critical_fail,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "evidence_kind": "post_launch_smoke",
        }
