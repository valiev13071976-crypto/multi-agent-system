"""Recovery orchestration runtime helpers and config."""

from __future__ import annotations

import os
from pathlib import Path

from recovery.orchestrator import RecoveryOrchestrator
from recovery.policy import RecoveryPolicy
from recovery.queue import RecoveryQueue
from recovery.store import (
    InMemoryRecoveryCaseStore,
    RecoveryPersistenceUnavailableError,
    SqliteRecoveryCaseStore,
    normalize_recovery_db_path,
)


def recovery_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = str(source.get("RECOVERY_ORCHESTRATION_ENABLED", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def recovery_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    return {
        "enabled": recovery_enabled(source),
        "max_read_checks": int(source.get("RECOVERY_MAX_READ_CHECKS", "3") or 3),
        "base_backoff_seconds": float(
            source.get("RECOVERY_BASE_BACKOFF_SECONDS", "5") or 5
        ),
        "max_backoff_seconds": float(
            source.get("RECOVERY_MAX_BACKOFF_SECONDS", "60") or 60
        ),
    }


def _paths_equal(left: str | Path | None, right: str | Path | None) -> bool:
    if left is None or right is None:
        return False
    try:
        return normalize_recovery_db_path(left) == normalize_recovery_db_path(right)
    except OSError:
        # Fallback when resolve fails (missing parent, etc.)
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
            os.path.abspath(str(right))
        )


def build_recovery_store(
    *,
    env: dict | None = None,
    db_path: str | Path | None = None,
    shared_connection=None,
    side_effect_db_path: str | Path | None = None,
    require_durable: bool = False,
):
    """Build recovery case store.

    Rules:
    A. shared sqlite available + RECOVERY_DB_PATH unset → shared connection
    B. RECOVERY_DB_PATH different path → dedicated connection
    C. RECOVERY_DB_PATH same as side-effect path → shared connection (no second open)
    D. memory side-effect + no explicit recovery path → in-memory
    """

    source = env if env is not None else os.environ
    recovery_raw = db_path if db_path is not None else source.get("RECOVERY_DB_PATH")
    recovery_path = str(recovery_raw).strip() if recovery_raw is not None else ""

    se_path = side_effect_db_path
    if se_path is None and shared_connection is not None:
        se_path = getattr(shared_connection, "path", None)

    if recovery_path:
        if shared_connection is not None and _paths_equal(recovery_path, se_path):
            return SqliteRecoveryCaseStore(
                shared_connection=shared_connection, owns_connection=False
            )
        return SqliteRecoveryCaseStore(db_path=recovery_path, owns_connection=True)

    if shared_connection is not None:
        return SqliteRecoveryCaseStore(
            shared_connection=shared_connection, owns_connection=False
        )

    if require_durable:
        raise RecoveryPersistenceUnavailableError("recovery_persistence_unavailable")

    return InMemoryRecoveryCaseStore()


def _fail_closed_orchestrator(cfg: dict, **kwargs) -> RecoveryOrchestrator:
    store = InMemoryRecoveryCaseStore()
    store.available = False
    orch = RecoveryOrchestrator(
        store=store,
        queue=RecoveryQueue(store, max_attempts=cfg["max_read_checks"]),
        policy=RecoveryPolicy(),
        enabled=True,
        max_read_checks=cfg["max_read_checks"],
        base_backoff_seconds=cfg["base_backoff_seconds"],
        max_backoff_seconds=cfg["max_backoff_seconds"],
        **kwargs,
    )
    orch._fail_closed_persistence()
    return orch


def build_recovery_orchestrator(
    *,
    env: dict | None = None,
    store=None,
    persistence=None,
    reconciliation_service=None,
    workflow_engine=None,
    gate=None,
    hitl=None,
    side_effect_executor=None,
    observability=None,
    audit=None,
) -> RecoveryOrchestrator | None:
    cfg = recovery_config(env)
    if not cfg["enabled"]:
        return None

    shared = None
    require_durable = False
    se_path = None
    if persistence is not None:
        if (
            getattr(persistence, "backend", None) == "sqlite"
            and getattr(persistence, "ready", False)
            and getattr(persistence, "connection", None) is not None
        ):
            shared = persistence.connection
            require_durable = True
            se_path = getattr(shared, "path", None) or getattr(
                persistence, "database_path_ref", None
            )
            # Prefer full path from connection over basename ref.
            se_path = getattr(shared, "path", None)

    kwargs = dict(
        reconciliation_service=reconciliation_service,
        workflow_engine=workflow_engine,
        gate=gate,
        hitl=hitl,
        side_effect_executor=side_effect_executor,
        observability=observability,
        audit=audit,
    )

    try:
        resolved = store or build_recovery_store(
            env=env,
            shared_connection=shared,
            side_effect_db_path=se_path,
            require_durable=require_durable,
        )
    except RecoveryPersistenceUnavailableError:
        if require_durable:
            return _fail_closed_orchestrator(cfg, **kwargs)
        raise

    queue = RecoveryQueue(resolved, max_attempts=cfg["max_read_checks"])
    return RecoveryOrchestrator(
        store=resolved,
        queue=queue,
        policy=RecoveryPolicy(),
        enabled=True,
        max_read_checks=cfg["max_read_checks"],
        base_backoff_seconds=cfg["base_backoff_seconds"],
        max_backoff_seconds=cfg["max_backoff_seconds"],
        **kwargs,
    )


def run_startup_recovery_materialization(
    orchestrator: RecoveryOrchestrator,
    *,
    execution_store,
    reconciliation_store=None,
    permit_store=None,
    budget_store=None,
    enqueue: bool = True,
) -> dict:
    """Local scan materialization. Callers must not perform network here."""
    return orchestrator.materialize_from_local_scan(
        execution_store=execution_store,
        reconciliation_store=reconciliation_store,
        permit_store=permit_store,
        budget_store=budget_store,
        enqueue=enqueue,
    )
