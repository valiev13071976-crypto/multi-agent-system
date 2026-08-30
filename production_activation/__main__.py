"""Stage-5 operator CLI harness.

Mutating actions require --confirm.
Does not fabricate LIVE_VERIFIED public production activation.
"""

from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace

from production_activation.commands import ActivateProductionCommand, AuthorizeActivationCommand, PrepareActivationCommand, RollbackProductionCommand
from production_activation.runtime import get_production_activation_runtime


def _admin(actor: str = "cli-operator"):
    return SimpleNamespace(
        actor_ref=lambda: actor,
        permissions=("operations:activation.read", "operations:activation.write", "operations:activation.authorize", "operations:read"),
        roles=("PLATFORM_ADMIN",),
    )


def _print(data) -> int:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage-5 production activation harness")
    parser.add_argument("--actor", default="cli-operator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Inspect Stage-5 status")

    p_preflight = sub.add_parser("validate-prerequisite", help="Verify Stage-3/4 handoff")
    p_preflight.add_argument("--candidate-id", required=True)

    p_eval = sub.add_parser("evaluate", help="Evaluate Stage-5 pre-activation gate")
    p_eval.add_argument("--candidate-id", default="")

    p_prepare = sub.add_parser("prepare", help="Lock candidate and create GoLivePlan")
    p_prepare.add_argument("--candidate-id", required=True)
    p_prepare.add_argument("--production-url", required=True)
    p_prepare.add_argument("--monitoring", required=True)
    p_prepare.add_argument("--alerts", required=True)
    p_prepare.add_argument("--confirm", action="store_true")

    p_auth = sub.add_parser("authorize", help="Issue activation authorization")
    p_auth.add_argument("--candidate-id", required=True)
    p_auth.add_argument("--plan-id", required=True)
    p_auth.add_argument("--idempotency-key", required=True)
    p_auth.add_argument("--confirm", action="store_true")

    p_activate = sub.add_parser("activate", help="Execute governed production activation")
    p_activate.add_argument("--candidate-id", required=True)
    p_activate.add_argument("--plan-id", required=True)
    p_activate.add_argument("--authorization-id", required=True)
    p_activate.add_argument("--confirmation-token", required=True)
    p_activate.add_argument("--idempotency-key", required=True)
    p_activate.add_argument("--confirm", action="store_true")

    p_deact = sub.add_parser("deactivate", help="Deactivate / roll back GO LIVE state")
    p_deact.add_argument("--candidate-id", required=True)
    p_deact.add_argument("--reason", default="operator_deactivate")
    p_deact.add_argument("--confirm", action="store_true")

    p_health = sub.add_parser("post-activation-health", help="Post-activation health classification")
    p_health.add_argument("--candidate-id", default="")

    p_accept = sub.add_parser("acceptance", help="Evaluate production acceptance")
    p_accept.add_argument("--candidate-id", required=True)

    p_seed = sub.add_parser("seed-evidence", help="Seed CODE_VERIFIED Stage-5 mandatory gates")
    p_seed.add_argument("--candidate-id", required=True)
    p_seed.add_argument("--release-identity", required=True)
    p_seed.add_argument("--confirm", action="store_true")

    p_policy = sub.add_parser("create-policy", help="Create disabled GoLivePolicy")
    p_policy.add_argument("--release-identity", required=True)
    p_policy.add_argument("--confirm", action="store_true")

    args = parser.parse_args(argv)
    svc = get_production_activation_runtime()
    ctx = _admin(args.actor)

    if args.cmd == "status":
        return _print(svc.stage5_status(ctx))
    if args.cmd == "validate-prerequisite":
        return _print(svc.preflight(ctx, args.candidate_id))
    if args.cmd == "evaluate":
        return _print(svc.evaluate_stage5_gate(ctx, candidate_id=args.candidate_id))
    if args.cmd == "prepare":
        if not args.confirm:
            print("Refusing: prepare requires --confirm", file=sys.stderr)
            return 2
        return _print(
            svc.prepare(
                ctx,
                PrepareActivationCommand(
                    candidate_id=args.candidate_id,
                    production_url=args.production_url,
                    operator_ref=ctx.actor_ref(),
                    monitoring_destination=args.monitoring,
                    alert_destination=args.alerts,
                ),
            )
        )
    if args.cmd == "authorize":
        if not args.confirm:
            print("Refusing: authorize requires --confirm", file=sys.stderr)
            return 2
        return _print(
            svc.authorize(
                ctx,
                AuthorizeActivationCommand(
                    candidate_id=args.candidate_id,
                    plan_id=args.plan_id,
                    operator_ref=ctx.actor_ref(),
                    idempotency_key=args.idempotency_key,
                ),
            )
        )
    if args.cmd == "activate":
        if not args.confirm:
            print("Refusing: activate requires --confirm", file=sys.stderr)
            return 2
        return _print(
            svc.activate(
                ctx,
                ActivateProductionCommand(
                    candidate_id=args.candidate_id,
                    plan_id=args.plan_id,
                    authorization_id=args.authorization_id,
                    operator_ref=ctx.actor_ref(),
                    confirmation_token=args.confirmation_token,
                    idempotency_key=args.idempotency_key,
                ),
            )
        )
    if args.cmd == "deactivate":
        if not args.confirm:
            print("Refusing: deactivate requires --confirm", file=sys.stderr)
            return 2
        return _print(svc.deactivate(ctx, candidate_id=args.candidate_id, operator_ref=ctx.actor_ref(), reason=args.reason))
    if args.cmd == "post-activation-health":
        return _print(svc.post_activation_health(ctx, candidate_id=args.candidate_id))
    if args.cmd == "acceptance":
        return _print(svc.evaluate_acceptance(ctx, candidate_id=args.candidate_id))
    if args.cmd == "seed-evidence":
        if not args.confirm:
            print("Refusing: seed-evidence requires --confirm", file=sys.stderr)
            return 2
        return _print(svc.seed_stage5_evidence(ctx, candidate_id=args.candidate_id, release_identity=args.release_identity))
    if args.cmd == "create-policy":
        if not args.confirm:
            print("Refusing: create-policy requires --confirm", file=sys.stderr)
            return 2
        return _print(svc.create_go_live_policy(ctx, release_identity=args.release_identity, created_by=args.actor))
    print(f"unknown command: {args.cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
