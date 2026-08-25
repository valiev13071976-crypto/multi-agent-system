"""Release gate — PASS / FAIL / BLOCKED."""

from __future__ import annotations

from dataclasses import dataclass, field

from evals.models import EvalBaseline, EvalRun

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
GATE_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ReleaseGateDecision:
    decision: str
    reason_codes: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "details", dict(self.details or {}))


class ReleaseGate:
    def evaluate(
        self,
        run: EvalRun,
        *,
        baseline: EvalBaseline | None = None,
        comparison: dict | None = None,
        version_mismatches: list | None = None,
        required_pass_rate: float = 1.0,
        blocked_reason: str | None = None,
    ) -> ReleaseGateDecision:
        if blocked_reason:
            return ReleaseGateDecision(
                GATE_BLOCKED,
                reason_codes=(blocked_reason,),
            )
        if run.status == "blocked":
            return ReleaseGateDecision(
                GATE_BLOCKED,
                reason_codes=("suite_blocked",),
            )

        reasons: list[str] = []
        details: dict = {}

        if run.critical_failures:
            reasons.append("critical_case_failed")
            details["critical_failures"] = list(run.critical_failures)

        if run.pass_rate < float(required_pass_rate):
            reasons.append("pass_rate_below_threshold")
            details["pass_rate"] = run.pass_rate
            details["required_pass_rate"] = required_pass_rate

        mismatches = list(version_mismatches or [])
        if mismatches:
            reasons.append("artifact_changed_without_version_bump")
            details["version_mismatches"] = mismatches

        for result in run.case_results:
            if result.case_id == "compat_analyze_public_keys" and not result.passed:
                reasons.append("compatibility_eval_failed")
            if "security" in (result.metadata_safe.get("category") or "") and (
                result.status == "failed" or result.status == "error"
            ):
                if "security_regression" not in reasons:
                    reasons.append("security_regression")
            if result.status == "error":
                if "deterministic_eval_error" not in reasons:
                    reasons.append("deterministic_eval_error")

        # Category-based security / compatibility from case results
        for result in run.case_results:
            cat = str(result.metadata_safe.get("category") or "")
            if not result.passed and result.status in {"failed", "error"}:
                if cat == "compatibility":
                    if "compatibility_eval_failed" not in reasons:
                        reasons.append("compatibility_eval_failed")
                if cat == "security" and result.critical:
                    if "security_regression" not in reasons:
                        reasons.append("security_regression")

        if comparison:
            critical_removed = list(comparison.get("critical_removed") or [])
            if critical_removed:
                reasons.append("critical_eval_case_removed")
                details["critical_removed"] = critical_removed
            regressions = list(comparison.get("regressions") or [])
            if regressions:
                reasons.append("baseline_regression")
                details["regressions"] = regressions

        if reasons:
            return ReleaseGateDecision(GATE_FAIL, reason_codes=tuple(reasons), details=details)
        return ReleaseGateDecision(GATE_PASS, reason_codes=())
