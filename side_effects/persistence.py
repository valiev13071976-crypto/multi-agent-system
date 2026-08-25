"""Factory and health for durable side-effect persistence."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from autonomy.idempotency import IdempotencyRegistry
from autonomy.models import sanitize_metadata, utc_now
from security.encryption import EncryptionService
from side_effects.errors import SideEffectPersistenceUnavailableError
from side_effects.recovery_scan import RecoveryScanResult, scan_recovery_candidates
from side_effects.schema import DEFAULT_DB_PATH, SCHEMA_VERSION
from side_effects.sqlite_store import (
    PersistentIdempotencyStore,
    PersistentReconciliationStore,
    PersistentSideEffectExecutionStore,
    SideEffectPersistenceUnitOfWork,
    SqliteConnection,
)
from side_effects.store import InMemorySideEffectExecutionStore
from side_effects.reconciliation_store import InMemoryReconciliationStore
from autonomy.store import InMemoryIdempotencyStore


def _meta(value) -> Mapping[str, object]:
    return MappingProxyType(sanitize_metadata(value))


@dataclass(frozen=True)
class SideEffectPersistenceBundle:
    backend: str
    ready: bool
    connection: SqliteConnection | None
    execution_store: object
    idempotency_store: object
    reconciliation_store: object
    idempotency_registry: IdempotencyRegistry
    encryption: EncryptionService | None
    schema_version: int | None
    database_path_ref: str | None
    unit_of_work_factory: object | None = None
    last_scan: RecoveryScanResult | None = None
    reason_code: str = "persistence_ready"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))

    def unit_of_work(self) -> SideEffectPersistenceUnitOfWork | None:
        if self.connection is None:
            return None
        return SideEffectPersistenceUnitOfWork(self.connection)


def _normalize_db_path(raw: str | None) -> str:
    text = str(raw or DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH
    if "://" in text:
        raise SideEffectPersistenceUnavailableError("side_effect_db_path_invalid")
    path = Path(text)
    if path.is_absolute():
        return str(path)
    return str(Path.cwd() / path)


def build_side_effect_persistence(
    *,
    env: dict | None = None,
    encryption: EncryptionService | None = None,
    durable: bool | None = None,
    db_path: str | None = None,
    run_recovery_scan: bool | None = None,
) -> SideEffectPersistenceBundle:
    """Build in-memory or SQLite persistence bundle.

    durable=None → enabled when SIDE_EFFECT_PERSISTENCE_BACKEND=sqlite or path set.
    """

    source = env if env is not None else os.environ
    backend = str(source.get("SIDE_EFFECT_PERSISTENCE_BACKEND") or "memory").strip().lower()
    path_raw = db_path if db_path is not None else source.get("SIDE_EFFECT_DB_PATH")
    if durable is None:
        durable = backend in {"sqlite", "durable"} or bool(
            path_raw and str(path_raw).strip()
        )
    if not durable:
        idem_store = InMemoryIdempotencyStore()
        return SideEffectPersistenceBundle(
            backend="memory",
            ready=True,
            connection=None,
            execution_store=InMemorySideEffectExecutionStore(),
            idempotency_store=idem_store,
            reconciliation_store=InMemoryReconciliationStore(),
            idempotency_registry=IdempotencyRegistry(idem_store),
            encryption=encryption,
            schema_version=None,
            database_path_ref=None,
            reason_code="persistence_memory",
        )
    try:
        path = _normalize_db_path(path_raw)
        connection = SqliteConnection(path)
        version = connection.initialize_schema()
        exec_store = PersistentSideEffectExecutionStore(
            connection, encryption=encryption
        )
        idem_store = PersistentIdempotencyStore(connection, encryption=encryption)
        recon_store = PersistentReconciliationStore(connection, encryption=encryption)
        registry = IdempotencyRegistry(idem_store)
        scan_flag = source.get("SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP")
        if run_recovery_scan is None:
            run_recovery_scan = True
            if scan_flag is not None and str(scan_flag).strip():
                run_recovery_scan = str(scan_flag).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
        scan = None
        if run_recovery_scan:
            scan = scan_recovery_candidates(
                execution_store=exec_store,
                idempotency_store=idem_store,
                reconciliation_store=recon_store,
            )
        path_ref = Path(path).name
        return SideEffectPersistenceBundle(
            backend="sqlite",
            ready=True,
            connection=connection,
            execution_store=exec_store,
            idempotency_store=idem_store,
            reconciliation_store=recon_store,
            idempotency_registry=registry,
            encryption=encryption,
            schema_version=version,
            database_path_ref=path_ref,
            last_scan=scan,
            reason_code="persistence_ready",
            metadata={
                "schema_version": version,
                "recovery_scan": None if scan is None else scan.as_dict(),
            },
        )
    except SideEffectPersistenceUnavailableError as exc:
        return SideEffectPersistenceBundle(
            backend="sqlite",
            ready=False,
            connection=None,
            execution_store=InMemorySideEffectExecutionStore(),
            idempotency_store=InMemoryIdempotencyStore(),
            reconciliation_store=InMemoryReconciliationStore(),
            idempotency_registry=IdempotencyRegistry(),
            encryption=encryption,
            schema_version=None,
            database_path_ref=None,
            reason_code=str(exc.error_code),
        )
    except Exception:
        return SideEffectPersistenceBundle(
            backend="sqlite",
            ready=False,
            connection=None,
            execution_store=InMemorySideEffectExecutionStore(),
            idempotency_store=InMemoryIdempotencyStore(),
            reconciliation_store=InMemoryReconciliationStore(),
            idempotency_registry=IdempotencyRegistry(),
            encryption=encryption,
            schema_version=None,
            database_path_ref=None,
            reason_code="side_effect_persistence_unavailable",
        )
