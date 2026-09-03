"""CLI bootstrap for protected OWNER — never prints password values."""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Panda OWNER account (idempotent).")
    parser.add_argument("--db", default=os.environ.get("ACCOUNTS_DB_PATH") or "data/accounts.sqlite")
    parser.add_argument("--env-only", action="store_true", help="Require PANDA_BOOTSTRAP_OWNER=true and env credentials")
    args = parser.parse_args(argv)

    # Ensure flag for service bootstrap_from_env
    env = dict(os.environ)
    if args.env_only and str(env.get("PANDA_BOOTSTRAP_OWNER") or "").lower() not in {"1", "true", "yes"}:
        print("bootstrap_skipped: PANDA_BOOTSTRAP_OWNER not enabled")
        return 2

    from accounts.runtime import build_accounts_runtime

    runtime = build_accounts_runtime(env={**env, "ACCOUNTS_DB_PATH": args.db})
    try:
        # Force bootstrap path when CLI invoked explicitly with credentials in env
        env["PANDA_BOOTSTRAP_OWNER"] = env.get("PANDA_BOOTSTRAP_OWNER") or "true"
        result = runtime.service.bootstrap_from_env(env)
        # Never include password in output
        safe = {k: v for k, v in result.items() if "password" not in k.lower()}
        print(safe)
        return 0 if result.get("bootstrapped") or result.get("reason") == "owner_already_exists" else 1
    finally:
        runtime.close()


if __name__ == "__main__":
    sys.exit(main())
