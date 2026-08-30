"""Process runtime roles for API / Worker / Combined modes."""

from __future__ import annotations

import os

ROLE_API = "api"
ROLE_WORKER = "worker"
ROLE_COMBINED = "combined"

VALID_ROLES = frozenset({ROLE_API, ROLE_WORKER, ROLE_COMBINED})


def resolve_runtime_role(env: dict | None = None) -> str:
    """Resolve RUNTIME_ROLE with backward-compatible WORKFLOW_WORKER_ENABLED.

    Defaults to combined (API + worker in one process) for local/dev.
    """

    source = env if env is not None else os.environ
    raw = str(source.get("RUNTIME_ROLE") or "").strip().lower()
    if raw in VALID_ROLES:
        return raw
    # Legacy: WORKFLOW_WORKER_ENABLED=false → API-only responsibilities.
    worker_flag = str(source.get("WORKFLOW_WORKER_ENABLED", "true")).strip().lower()
    if worker_flag in {"0", "false", "no", "off"}:
        return ROLE_API
    return ROLE_COMBINED


def role_runs_worker_loops(role: str) -> bool:
    """Worker claim / scheduler tick / recovery loops."""

    return role in {ROLE_WORKER, ROLE_COMBINED}


def role_accepts_http(role: str) -> bool:
    return role in {ROLE_API, ROLE_COMBINED}


def workflow_worker_enabled(env: dict | None = None) -> bool:
    """True when this process should run background worker loops."""

    return role_runs_worker_loops(resolve_runtime_role(env))
