"""Human + machine-readable eval reports."""

from __future__ import annotations

import json
from pathlib import Path

from evals.models import EvalRun, canonical_json
from evals.release_gate import ReleaseGateDecision


def format_report(
    run: EvalRun,
    gate: ReleaseGateDecision,
    *,
    manifest_hash: str = "",
) -> str:
    lines = [
        f"suite={run.suite_id} version={run.suite_version}",
        f"run_id={run.run_id}",
        f"manifest_hash={manifest_hash or run.artifact_versions.get('manifest_hash', '')}",
        f"total={run.total} passed={run.passed} failed={run.failed} skipped={run.skipped}",
        f"pass_rate={run.pass_rate:.4f}",
        f"critical_failures={list(run.critical_failures)}",
        f"regressions={list(run.regressions)}",
        f"gate={gate.decision}",
    ]
    if gate.reason_codes:
        lines.append(f"gate_reasons={list(gate.reason_codes)}")
    return "\n".join(lines) + "\n"


def run_to_json_dict(
    run: EvalRun,
    gate: ReleaseGateDecision,
    *,
    manifest_hash: str = "",
) -> dict:
    return {
        "run_id": run.run_id,
        "suite_id": run.suite_id,
        "suite_version": run.suite_version,
        "status": run.status,
        "total": run.total,
        "passed": run.passed,
        "failed": run.failed,
        "skipped": run.skipped,
        "pass_rate": run.pass_rate,
        "critical_failures": list(run.critical_failures),
        "regressions": list(run.regressions),
        "improvements": list(run.improvements),
        "git_commit": run.git_commit,
        "environment_ref": run.environment_ref,
        "artifact_versions": dict(run.artifact_versions),
        "manifest_hash": manifest_hash
        or run.artifact_versions.get("manifest_hash", ""),
        "gate": {
            "decision": gate.decision,
            "reason_codes": list(gate.reason_codes),
            "details": dict(gate.details),
        },
        "cases": [
            {
                "case_id": r.case_id,
                "status": r.status,
                "passed": r.passed,
                "score": r.score,
                "critical": r.critical,
                "reason_codes": list(r.reason_codes),
                "duration_ms": r.duration_ms,
                "actual_summary_safe": dict(r.actual_summary_safe),
                "expected_summary_safe": dict(r.expected_summary_safe),
            }
            for r in run.case_results
        ],
    }


def write_json_report(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def write_text_report(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
