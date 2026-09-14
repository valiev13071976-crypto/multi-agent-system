#!/usr/bin/env python3
"""Runs the live semantic evaluation for the managed-agent-runtime POC
against a REAL OpenAI model -- never ``ScriptedModel``, never a mocked
or hardcoded tool decision.

STOPS IMMEDIATELY, before making a single subprocess or model call, if
``OPENAI_API_KEY`` is not present in the environment. This is
deliberate and required: no live evaluation result may ever be claimed
without a real model run, and this script must never invent a
workaround (no fallback to a scripted decision, no hardcoded key).

Usage:

    python3 managed_agent_poc/scripts/run_live_eval.py [--out-dir DIR]

Requires the isolated SDK install (see
``managed_agent_poc/scripts/setup_isolated_env.py``) and
``OPENAI_API_KEY`` set in the environment. Deliberately sets
``PANDA_MANAGED_AGENT_POC_ENABLED=true`` for its own process only --
running this script IS the explicit opt-in the flag exists to gate;
nothing else is affected, since production code never imports this
package.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ["PANDA_MANAGED_AGENT_POC_ENABLED"] = "true"

from managed_agent_poc.adapter import ManagedAgentPOC  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=None, help="Directory to write the evaluation report + scratch SQLite files to (default: a fresh temp dir).")
    args = parser.parse_args()

    available, reason = ManagedAgentPOC.real_model_available()
    if not available:
        print("LIVE EVALUATION STOPPED -- no real model call was attempted.")
        print(f"Reason: {reason}")
        print("Add OPENAI_API_KEY via Cloud Agents -> Secrets to run this live.")
        return 1

    # Import here, not at module top-level, so the availability check above
    # always runs first regardless of import cost/side effects below.
    from managed_agent_poc.live_eval import report, runner
    from managed_agent_poc.live_eval.scoring import compute_accuracy

    out_dir = args.out_dir or tempfile.mkdtemp(prefix="panda_managed_agent_live_eval_")
    os.makedirs(out_dir, exist_ok=True)

    print(f"Running live evaluation (real model calls, work dir: {out_dir}) ...")
    results = runner.run_all(work_dir=out_dir)
    accuracy = compute_accuracy(results)

    real_model = next((r.model for r in results if r.model), "(unknown)")
    markdown = report.render_markdown(results, accuracy, real_model=real_model, real_calls=len(results))

    report_path = os.path.join(out_dir, "live_eval_report.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(markdown)

    print(markdown)
    print(f"\nReport written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
