"""Recovery orchestration runtime helpers and config."""

from __future__ import annotations

import os
from pathlib import Path

from recovery.orchestrator import RecoveryOrchestrator
from recovery.policy import RecoveryPolicy
from recovery.queue import RecoveryQueue
from recovery.store import InMemoryRecoveryCaseStore, SqliteRecoveryCaseStore


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


def build_recovery_store(*, env: dict | None = None, db_path: str | Path | None = None):
    """Build recovery case store.

    Durable sqlite only when RECOVERY_DB_PATH (or db_path) is explicit.
    Avoids opening a second long-lived handle on SIDE_EFFECT_DB_PATH by default
    (Windows file locks + compose TemporaryDirectory cleanup).
    Restart rematerialization still rebuilds cases from local execution scan.
    """

    source = env if env is not None else os.environ
    path = db_path if db_path is not None else source.get("RECOVERY_DB_PATH")
    if path and str(path).strip():
        return SqliteRecoveryCaseStore(str(path).strip())
    return InMemoryRecoveryCaseStore()


def build_recovery_orchestrator(
    *,
    env: dict | None = None,
    store=None,
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
    store = store or build_recovery_store(env=env)
    queue = RecoveryQueue(store, max_attempts=cfg["max_read_checks"])
    return RecoveryOrchestrator(
        store=store,
        queue=queue,
        policy=RecoveryPolicy(),
        reconciliation_service=reconciliation_service,
        workflow_engine=workflow_engine,
        gate=gate,
        hitl=hitl,
        side_effect_executor=side_effect_executor,
        observability=observability,
        audit=audit,
        enabled=True,
        max_read_checks=cfg["max_read_checks"],
        base_backoff_seconds=cfg["base_backoff_seconds"],
        max_backoff_seconds=cfg["max_backoff_seconds"],
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
