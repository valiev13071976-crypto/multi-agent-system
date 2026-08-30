"""Stage-3 validation CLI."""

from __future__ import annotations

import argparse
import json
import sys

from production_validation.backup_drill import BackupDrillHarness
from production_validation.config import ValidationConfig
from production_validation.failure_harness import FailureInjectionHarness
from production_validation.isolation_harness import IsolationHarness
from production_validation.live_deployment import DeploymentValidator
from production_validation.load_harness import LoadHarness
from production_validation.operator_evidence import OperatorEvidenceError, OperatorEvidenceRecorder
from production_validation.providers_live import LiveProviderValidator
from production_validation.recovery_harness import RecoveryHarness
from production_validation.release_gate import ReleaseGateEvaluator
from production_validation.security_probes import SecurityProbeHarness
from production_validation.smoke import SmokeRunner
from production_validation.soak_harness import SoakHarness


def _run_local_suite(config: ValidationConfig) -> dict:
    from tests.test_smoke import load_app

    app = load_app().app
    results = {}
    results["3.3_smoke"] = SmokeRunner(config=config).run_local(app)["status"]
    results["3.5_security"] = SecurityProbeHarness(config=config).run_local()["status"]
    results["3.6_load"] = LoadHarness(config=config).run_local()["status"]
    results["3.7_isolation"] = IsolationHarness(config=config).run()["status"]
    results["3.8_soak"] = SoakHarness(config=config).run_local()["status"]
    results["3.9_failure_injection"] = FailureInjectionHarness(config=config).run()["status"]
    results["3.10_crash_recovery"] = RecoveryHarness(config=config).run_worker_crash_simulation()["status"]
    results["3.11_backup_restore"] = BackupDrillHarness(config=config).run_isolated()["status"]
    results["3.1_env_config"] = DeploymentValidator(config=config).validate_config()["status"]
    results["3.2_providers"] = LiveProviderValidator(config=config).run_gate()["status"]
    gate = ReleaseGateEvaluator(config=config).evaluate(local_results=results)
    results["3.17_release_gate"] = gate.verdict
    return {"local": results, "gate": gate.__dict__}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage-3 production validation harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("local", help="Run local/fixture validation suite")
    smoke = sub.add_parser("smoke", help="Run smoke tests")
    smoke.add_argument("--live", action="store_true")
    sub.add_parser("gate", help="Evaluate release readiness gate")
    sub.add_parser("backup-drill", help="Run isolated backup/restore drill")
    record = sub.add_parser("record-live", help="Record operator-attested Stage-3 live evidence")
    record.add_argument("--gate", required=True)
    record.add_argument("--status", required=True)
    record.add_argument("--operator", required=True)
    record.add_argument("--note", required=True)
    record.add_argument("--confirm-live-verified", action="store_true")
    record.add_argument("--release-identity", default="")
    record.add_argument("--artifact-ref", default="")

    args = parser.parse_args(argv)
    config = ValidationConfig.from_env()

    if args.cmd == "local":
        out = _run_local_suite(config)
        print(json.dumps(out, indent=2))
        failed = [k for k, v in out["local"].items() if v not in {"PASS", "BLOCKED", "SKIP"} and k != "3.17_release_gate"]
        return 1 if failed else 0

    if args.cmd == "smoke":
        if args.live:
            out = SmokeRunner(config=config).run_live()
        else:
            from tests.test_smoke import load_app

            out = SmokeRunner(config=config).run_local(load_app().app)
        print(json.dumps(out, indent=2))
        return 0 if out.get("status") in {"PASS", "BLOCKED"} else 1

    if args.cmd == "gate":
        gate = ReleaseGateEvaluator(config=config).evaluate()
        print(json.dumps(gate.__dict__, indent=2))
        return 0 if gate.verdict == "PRODUCTION_VALIDATION_PASS" else 1

    if args.cmd == "backup-drill":
        out = BackupDrillHarness(config=config).run_isolated()
        print(json.dumps(out, indent=2))
        return 0 if out.get("status") == "PASS" else 1

    if args.cmd == "record-live":
        try:
            out = OperatorEvidenceRecorder(config=config).record(
                gate=args.gate,
                status=args.status,
                operator=args.operator,
                note=args.note,
                confirm_live_verified=bool(args.confirm_live_verified),
                release_identity=str(args.release_identity or ""),
                artifact_ref=str(args.artifact_ref or ""),
            )
        except OperatorEvidenceError as exc:
            print(json.dumps({"error": exc.code, "message": exc.message, "details": exc.details}, indent=2))
            return 1
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
