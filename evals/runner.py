"""EvalRunner — offline deterministic suite execution."""

from __future__ import annotations

import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Callable

from evals.baseline import compare_to_baseline
from evals.handlers import get_handler
from evals.manifest import (
    assert_version_bumped_if_content_changed,
    build_artifact_manifest,
    build_version_registry,
)
from evals.models import (
    ArtifactVersion,
    EvalBaseline,
    EvalCase,
    EvalCaseResult,
    EvalRun,
    EvalSuite,
    utc_now,
)
from evals.release_gate import GATE_BLOCKED, GATE_FAIL, GATE_PASS, ReleaseGate
from evals.scoring import binary_score


def _git_commit() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        )
        return out.strip() or None
    except Exception:
        return None


def _git_dirty() -> bool | None:
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        )
        return bool(out.strip())
    except Exception:
        return None


class EvalRunner:
    def __init__(
        self,
        *,
        allow_network: bool = False,
        default_timeout_seconds: float = 30.0,
        observability=None,
    ):
        self.allow_network = allow_network
        self.default_timeout_seconds = default_timeout_seconds
        self.observability = observability
        self.release_gate = ReleaseGate()

    def run_case(
        self,
        case: EvalCase,
        *,
        run_id: str,
        timeout_seconds: float | None = None,
    ) -> EvalCaseResult:
        started = time.perf_counter()
        if case.requires_network and not self.allow_network:
            return EvalCaseResult(
                run_id=run_id,
                case_id=case.case_id,
                status="skipped",
                passed=True,
                score=1.0,
                reason_codes=("network_eval_disabled",),
                duration_ms=0,
                critical=case.critical,
                metadata_safe={"category": case.category},
                expected_summary_safe=dict(case.expected),
                actual_summary_safe={"skipped": True},
            )

        timeout = float(
            case.constraints.get("timeout_seconds")
            if case.constraints.get("timeout_seconds") is not None
            else (timeout_seconds or self.default_timeout_seconds)
        )
        try:
            handler = get_handler(case.handler)
        except KeyError:
            return EvalCaseResult(
                run_id=run_id,
                case_id=case.case_id,
                status="error",
                passed=False,
                score=0.0,
                reason_codes=("unknown_handler",),
                duration_ms=int((time.perf_counter() - started) * 1000),
                critical=case.critical,
                metadata_safe={"category": case.category},
            )

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(handler, case)
                payload = fut.result(timeout=timeout)
        except FuturesTimeout:
            return EvalCaseResult(
                run_id=run_id,
                case_id=case.case_id,
                status="error",
                passed=False,
                score=0.0,
                reason_codes=("timeout",),
                duration_ms=int((time.perf_counter() - started) * 1000),
                critical=case.critical,
                metadata_safe={"category": case.category},
            )
        except Exception as exc:
            return EvalCaseResult(
                run_id=run_id,
                case_id=case.case_id,
                status="error",
                passed=False,
                score=0.0,
                reason_codes=("handler_exception",),
                duration_ms=int((time.perf_counter() - started) * 1000),
                critical=case.critical,
                metadata_safe={
                    "category": case.category,
                    "error_type": type(exc).__name__,
                },
                actual_summary_safe={"error": type(exc).__name__},
            )

        passed = bool(payload.get("passed"))
        status = "passed" if passed else "failed"
        score = float(payload.get("score", binary_score(passed)))
        return EvalCaseResult(
            run_id=run_id,
            case_id=case.case_id,
            status=status,
            passed=passed,
            score=score,
            reason_codes=tuple(payload.get("reason_codes") or ()),
            duration_ms=int((time.perf_counter() - started) * 1000),
            artifact_versions=dict(payload.get("artifact_versions") or {}),
            actual_summary_safe=dict(payload.get("actual") or {}),
            expected_summary_safe=dict(case.expected),
            critical=case.critical,
            metadata_safe={"category": case.category},
        )

    def run_suite(
        self,
        suite: EvalSuite,
        *,
        baseline: EvalBaseline | None = None,
        run_id: str | None = None,
    ) -> tuple[EvalRun, object]:
        rid = run_id or str(uuid.uuid4())
        started_at = utc_now()
        results: list[EvalCaseResult] = []
        ordered = tuple(sorted(suite.cases, key=lambda c: c.case_id))

        for case in ordered:
            results.append(self.run_case(case, run_id=rid))

        passed = sum(1 for r in results if r.status == "passed")
        failed = sum(1 for r in results if r.status in {"failed", "error"})
        skipped = sum(1 for r in results if r.status == "skipped")
        # Skipped network cases count as passed for pass_rate denom of executed?
        # Spec: pass_rate over runnable; skipped don't fail. Use
        # executed = total - skipped; pass_rate = passed_executed / executed
        executed = [r for r in results if r.status != "skipped"]
        executed_pass = sum(1 for r in executed if r.passed and r.status == "passed")
        pass_rate = (
            (executed_pass / len(executed)) if executed else 1.0
        )
        critical_failures = tuple(
            r.case_id
            for r in results
            if r.critical and r.status in {"failed", "error"}
        )

        manifest = build_artifact_manifest()
        version_mismatches = self._detect_version_mismatches(baseline, manifest)

        comparison = None
        regressions: tuple[str, ...] = ()
        improvements: tuple[str, ...] = ()
        if baseline is not None:
            comparison = compare_to_baseline(
                # temporary run for compare — build after status
                EvalRun(
                    run_id=rid,
                    suite_id=suite.suite_id,
                    suite_version=suite.suite_version,
                    started_at=started_at,
                    completed_at=utc_now(),
                    total=len(results),
                    passed=passed,
                    failed=failed,
                    skipped=skipped,
                    pass_rate=pass_rate,
                    critical_failures=critical_failures,
                    status="passed",
                    case_results=tuple(results),
                ),
                baseline,
            )
            regressions = tuple(comparison.get("regressions") or ())
            improvements = tuple(comparison.get("improvements") or ())

        status = "passed"
        if critical_failures or failed or version_mismatches:
            status = "failed"
        if comparison and comparison.get("critical_removed"):
            status = "failed"

        run = EvalRun(
            run_id=rid,
            suite_id=suite.suite_id,
            suite_version=suite.suite_version,
            started_at=started_at,
            completed_at=utc_now(),
            total=len(results),
            passed=passed,
            failed=failed,
            skipped=skipped,
            pass_rate=pass_rate,
            critical_failures=critical_failures,
            status=status,
            case_results=tuple(results),
            git_commit=_git_commit(),
            environment_ref="offline" if not self.allow_network else "network",
            baseline_reference=baseline.baseline_id if baseline else None,
            regressions=regressions,
            improvements=improvements,
            artifact_versions={
                "manifest_hash": manifest.get("manifest_hash", ""),
                "suite_version": suite.suite_version,
                "suite_content_hash": suite.content_hash,
            },
            metadata_safe={
                "git_dirty": _git_dirty(),
                "allow_network": self.allow_network,
            },
        )

        gate = self.release_gate.evaluate(
            run,
            baseline=baseline,
            comparison=comparison,
            version_mismatches=version_mismatches,
            required_pass_rate=suite.required_pass_rate,
        )
        return run, gate

    def compare_to_baseline(self, run: EvalRun, baseline: EvalBaseline) -> dict:
        return compare_to_baseline(run, baseline)

    def _detect_version_mismatches(
        self, baseline: EvalBaseline | None, manifest: dict
    ) -> list:
        if baseline is None:
            return []
        mismatches = []
        base_arts = baseline.artifact_versions.get("artifacts")
        if not isinstance(base_arts, list):
            return []
        current = {
            (a["artifact_type"], a["artifact_id"], a["version"]): a["content_hash"]
            for a in manifest.get("artifacts", [])
        }
        for row in base_arts:
            key = (row["artifact_type"], row["artifact_id"], row["version"])
            cur_hash = current.get(key)
            if cur_hash is not None and cur_hash != row["content_hash"]:
                mismatches.append(
                    {
                        "artifact_type": row["artifact_type"],
                        "artifact_id": row["artifact_id"],
                        "version": row["version"],
                        "reason": "artifact_changed_without_version_bump",
                    }
                )
        return mismatches
