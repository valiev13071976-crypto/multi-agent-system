"""Baseline save/load and comparison."""

from __future__ import annotations

import json
from pathlib import Path

from evals.models import EvalBaseline, EvalRun, utc_now


def baseline_from_run(
    run: EvalRun, *, baseline_id: str, reference_commit: str | None = None
) -> EvalBaseline:
    outcomes = {
        r.case_id: {
            "status": r.status,
            "passed": r.passed,
            "critical": r.critical,
            "reason_codes": list(r.reason_codes),
        }
        for r in run.case_results
    }
    critical_ids = tuple(
        r.case_id for r in run.case_results if r.critical
    )
    return EvalBaseline(
        baseline_id=baseline_id,
        suite_id=run.suite_id,
        suite_version=run.suite_version,
        reference_commit=reference_commit or run.git_commit,
        summary={
            "total": run.total,
            "passed": run.passed,
            "failed": run.failed,
            "skipped": run.skipped,
            "pass_rate": run.pass_rate,
            "status": run.status,
        },
        case_outcomes=outcomes,
        artifact_versions=dict(run.artifact_versions),
        created_at=utc_now(),
        critical_case_ids=critical_ids,
    )


def save_baseline(baseline: EvalBaseline, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "baseline_id": baseline.baseline_id,
        "suite_id": baseline.suite_id,
        "suite_version": baseline.suite_version,
        "reference_commit": baseline.reference_commit,
        "summary": dict(baseline.summary),
        "case_outcomes": dict(baseline.case_outcomes),
        "artifact_versions": dict(baseline.artifact_versions),
        "created_at": baseline.created_at.isoformat(),
        "critical_case_ids": list(baseline.critical_case_ids),
    }
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_baseline(path: str | Path) -> EvalBaseline:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    from datetime import datetime

    created = datetime.fromisoformat(data["created_at"])
    return EvalBaseline(
        baseline_id=data["baseline_id"],
        suite_id=data["suite_id"],
        suite_version=data["suite_version"],
        reference_commit=data.get("reference_commit"),
        summary=data.get("summary") or {},
        case_outcomes=data.get("case_outcomes") or {},
        artifact_versions=data.get("artifact_versions") or {},
        created_at=created,
        critical_case_ids=tuple(data.get("critical_case_ids") or ()),
    )


def compare_to_baseline(run: EvalRun, baseline: EvalBaseline) -> dict:
    current = {r.case_id: r for r in run.case_results}
    base = dict(baseline.case_outcomes)
    classifications = {}
    regressions = []
    improvements = []
    new_cases = []
    removed_cases = []

    for case_id, result in current.items():
        prior = base.get(case_id)
        if prior is None:
            classifications[case_id] = "new_case"
            new_cases.append(case_id)
            continue
        prior_pass = bool(prior.get("passed"))
        if prior_pass and result.passed:
            classifications[case_id] = "unchanged_pass"
        elif (not prior_pass) and (not result.passed):
            classifications[case_id] = "unchanged_fail"
        elif prior_pass and not result.passed:
            classifications[case_id] = "regression"
            regressions.append(case_id)
        else:
            classifications[case_id] = "improvement"
            improvements.append(case_id)

    for case_id in base:
        if case_id not in current:
            classifications[case_id] = "removed_case"
            removed_cases.append(case_id)

    critical_removed = [
        cid for cid in baseline.critical_case_ids if cid not in current
    ]

    return {
        "classifications": classifications,
        "regressions": regressions,
        "improvements": improvements,
        "new_cases": new_cases,
        "removed_cases": removed_cases,
        "critical_removed": sorted(set(critical_removed)),
    }
