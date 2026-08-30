"""Stage-5 operator CLI harness."""

from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace

from production_activation.commands import ActivateProductionCommand, AuthorizeActivationCommand, PrepareActivationCommand
from production_activation.runtime import get_production_activation_runtime


def _admin():
    return SimpleNamespace(
        actor_ref=lambda: "cli-operator",
        permissions=("operations:activation.read", "operations:activation.write", "operations:activation.authorize", "operations:read"),
        roles=("PLATFORM_ADMIN",),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage-5 production activation harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_preflight = sub.add_parser("preflight", help="Verify Stage-3/4 handoff")
    p_preflight.add_argument("--candidate-id", required=True)

    p_prepare = sub.add_parser("prepare", help="Lock candidate and create GoLivePlan")
    p_prepare.add_argument("--candidate-id", required=True)
    p_prepare.add_argument("--production-url", required=True)
    p_prepare.add_argument("--monitoring", required=True)
    p_prepare.add_argument("--alerts", required=True)

    p_auth = sub.add_parser("authorize", help="Issue activation authorization")
    p_auth.add_argument("--candidate-id", required=True)
    p_auth.add_argument("--plan-id", required=True)
    p_auth.add_argument("--idempotency-key", required=True)

    p_activate = sub.add_parser("activate", help="Execute production activation")
    p_activate.add_argument("--candidate-id", required=True)
    p_activate.add_argument("--plan-id", required=True)
    p_activate.add_argument("--authorization-id", required=True)
    p_activate.add_argument("--confirmation-token", required=True)
    p_activate.add_argument("--idempotency-key", required=True)

    p_accept = sub.add_parser("acceptance", help="Evaluate production acceptance")
    p_accept.add_argument("--candidate-id", required=True)

    args = parser.parse_args(argv)
    svc = get_production_activation_runtime()
    ctx = _admin()

    if args.cmd == "preflight":
        out = svc.preflight(ctx, args.candidate_id)
    elif args.cmd == "prepare":
        out = svc.prepare(
            ctx,
            PrepareActivationCommand(
                candidate_id=args.candidate_id,
                production_url=args.production_url,
                operator_ref=ctx.actor_ref(),
                monitoring_destination=args.monitoring,
                alert_destination=args.alerts,
            ),
        )
    elif args.cmd == "authorize":
        out = svc.authorize(
            ctx,
            AuthorizeActivationCommand(
                candidate_id=args.candidate_id,
                plan_id=args.plan_id,
                operator_ref=ctx.actor_ref(),
                idempotency_key=args.idempotency_key,
            ),
        )
    elif args.cmd == "activate":
        out = svc.activate(
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
    elif args.cmd == "acceptance":
        out = svc.evaluate_acceptance(ctx, candidate_id=args.candidate_id)
    else:
        parser.error(f"unknown command: {args.cmd}")
        return 2

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
