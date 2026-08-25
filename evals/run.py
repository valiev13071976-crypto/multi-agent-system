"""CLI: python -m evals.run --suite core --no-network

Exit codes:
  0 = PASS
  1 = FAIL
  2 = BLOCKED / config error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.baseline import load_baseline
from evals.release_gate import GATE_BLOCKED, GATE_FAIL, GATE_PASS
from evals.report import format_report, run_to_json_dict, write_json_report, write_text_report
from evals.runner import EvalRunner
from evals.suites import get_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P10 offline eval runner")
    parser.add_argument("--suite", default="core")
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument(
        "--no-network",
        action="store_true",
        default=True,
        help="Disable network evals (default: on)",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        default=False,
        help="Allow cases marked requires_network",
    )
    args = parser.parse_args(argv)

    allow_network = bool(args.allow_network)

    try:
        suite = get_suite(args.suite)
    except KeyError:
        print(f"unknown suite: {args.suite}", file=sys.stderr)
        return 2

    baseline = None
    if args.baseline:
        try:
            baseline = load_baseline(args.baseline)
        except Exception as exc:
            print(f"baseline load failed: {type(exc).__name__}", file=sys.stderr)
            return 2

    runner = EvalRunner(allow_network=allow_network)
    try:
        run, gate = runner.run_suite(suite, baseline=baseline)
    except Exception as exc:
        print(f"eval blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    text = format_report(
        run,
        gate,
        manifest_hash=str(run.artifact_versions.get("manifest_hash", "")),
    )
    print(text, end="")

    if args.report:
        report_path = Path(args.report)
        write_text_report(report_path.with_suffix(".txt"), text)
        json_path = (
            report_path
            if report_path.suffix == ".json"
            else report_path.with_suffix(".json")
        )
        write_json_report(
            json_path,
            run_to_json_dict(
                run,
                gate,
                manifest_hash=str(run.artifact_versions.get("manifest_hash", "")),
            ),
        )

    if gate.decision == GATE_PASS:
        return 0
    if gate.decision == GATE_BLOCKED:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
