"""Operator CLI for Stage-4 Controlled Launch.

Mutating actions require explicit --confirm.
Does not activate Stage 5 / GO LIVE.
"""

from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace

from controlled_launch.access import PERM_LAUNCH_READ, PERM_LAUNCH_WRITE
from controlled_launch.runtime import get_controlled_launch_runtime


def _ctx(actor: str):
    return SimpleNamespace(
        actor_ref=actor,
        permissions=(PERM_LAUNCH_READ, PERM_LAUNCH_WRITE, "operations:read", "operations:write"),
        roles=("PLATFORM_ADMIN",),
        tenant_id="platform",
        request_id="cli",
    )


def _print(data) -> int:
    print(json.dumps(data, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m controlled_launch", description="Stage-4 Controlled Launch operator CLI")
    parser.add_argument("--actor", default="cli-operator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Inspect Stage-4 status")
    sub.add_parser("validate-prerequisite", help="Validate Stage-3 prerequisite (read-only)")

    p_create = sub.add_parser("create-policy", help="Create disabled ControlledLaunchPolicy")
    p_create.add_argument("--release-identity", required=True)
    p_create.add_argument("--tenant", action="append", default=[])
    p_create.add_argument("--max-cohort", type=int, default=10)
    p_create.add_argument("--max-traffic-percent", type=int, default=5)
    p_create.add_argument("--budget-ceiling", type=float, default=50.0)
    p_create.add_argument("--confirm", action="store_true")

    p_act = sub.add_parser("activate", help="Activate controlled launch for a policy")
    p_act.add_argument("--policy-id", required=True)
    p_act.add_argument("--confirm", action="store_true")

    p_pause = sub.add_parser("pause", help="Pause controlled launch")
    p_pause.add_argument("--policy-id", required=True)
    p_pause.add_argument("--confirm", action="store_true")

    p_kill = sub.add_parser("kill", help="Trigger Stage-4 kill switch")
    p_kill.add_argument("--policy-id", required=True)
    p_kill.add_argument("--reason", default="")
    p_kill.add_argument("--confirm", action="store_true")

    p_seed = sub.add_parser("seed-evidence", help="Seed CODE_VERIFIED Stage-4 mandatory gates")
    p_seed.add_argument("--candidate-id", required=True)
    p_seed.add_argument("--release-identity", required=True)
    p_seed.add_argument("--confirm", action="store_true")

    p_eval = sub.add_parser("evaluate", help="Evaluate Stage-4 release gate")
    p_eval.add_argument("--candidate-id", default="")

    args = parser.parse_args(argv)
    svc = get_controlled_launch_runtime()
    ctx = _ctx(args.actor)

    if args.cmd == "status":
        return _print(svc.stage4_status(ctx))
    if args.cmd == "validate-prerequisite":
        return _print(svc.get_handoff())
    if args.cmd == "create-policy":
        if not args.confirm:
            print("Refusing: create-policy requires --confirm", file=sys.stderr)
            return 2
        return _print(
            svc.create_launch_policy(
                ctx,
                release_identity=args.release_identity,
                tenant_allowlist=args.tenant,
                max_cohort_size=args.max_cohort,
                max_traffic_percent=args.max_traffic_percent,
                budget_ceiling=args.budget_ceiling,
                created_by=args.actor,
            )
        )
    if args.cmd == "activate":
        if not args.confirm:
            print("Refusing: activate requires --confirm", file=sys.stderr)
            return 2
        return _print(svc.activate_controlled_launch(ctx, policy_id=args.policy_id))
    if args.cmd == "pause":
        if not args.confirm:
            print("Refusing: pause requires --confirm", file=sys.stderr)
            return 2
        return _print(svc.pause_controlled_launch(ctx, policy_id=args.policy_id))
    if args.cmd == "kill":
        if not args.confirm:
            print("Refusing: kill requires --confirm", file=sys.stderr)
            return 2
        return _print(svc.kill_controlled_launch(ctx, policy_id=args.policy_id, reason=args.reason))
    if args.cmd == "seed-evidence":
        if not args.confirm:
            print("Refusing: seed-evidence requires --confirm", file=sys.stderr)
            return 2
        return _print(
            svc.seed_stage4_evidence(
                ctx,
                candidate_id=args.candidate_id,
                release_identity=args.release_identity,
            )
        )
    if args.cmd == "evaluate":
        return _print(svc.evaluate_stage4_gate(ctx, candidate_id=args.candidate_id))
    print(f"unknown command: {args.cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
