"""Factory and health for durable side-effect + protected-state persistence."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from autonomy.idempotency import IdempotencyRegistry
from autonomy.models import sanitize_metadata
from autonomy.store import InMemoryApprovalStore, InMemoryIdempotencyStore
from hitl.store import InMemoryExecutionPermitStore
from security.encryption import EncryptionService
from side_effects.errors import SideEffectPersistenceUnavailableError
from side_effects.protected_state_store import (
    PersistentApprovalStore,
    PersistentExecutionPermitStore,
    PersistentWorkflowRuntimeStore,
)
from side_effects.recovery_scan import RecoveryScanResult, scan_recovery_candidates
from side_effects.schema import DEFAULT_DB_PATH
from side_effects.sqlite_store import (
    PersistentIdempotencyStore,
    PersistentReconciliationStore,
    PersistentSideEffectExecutionStore,
    SideEffectPersistenceUnitOfWork,
    SqliteConnection,
)
from side_effects.store import InMemorySideEffectExecutionStore
from side_effects.reconciliation_store import InMemoryReconciliationStore
from task_queue.store import InMemoryTaskQueueStore
from workflow.schedule import InMemoryScheduleStore
from workflow.store import InMemoryWorkflowStateStore


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
    approval_store: object | None = None
    permit_store: object | None = None
    workflow_runtime_store: object | None = None
    schedule_store: object | None = None
    task_queue_store: object | None = None
    protected_state_ready: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "metadata", _meta(self.metadata))
        if self.approval_store is None:
            object.__setattr__(self, "approval_store", InMemoryApprovalStore())
        if self.permit_store is None:
            object.__setattr__(self, "permit_store", InMemoryExecutionPermitStore())
        if self.workflow_runtime_store is None:
            object.__setattr__(
                self, "workflow_runtime_store", InMemoryWorkflowStateStore()
            )
        if self.schedule_store is None:
            object.__setattr__(self, "schedule_store", InMemoryScheduleStore())
        if self.task_queue_store is None:
            object.__setattr__(self, "task_queue_store", InMemoryTaskQueueStore())

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


def _memory_bundle(
    *,
    encryption: EncryptionService | None,
    reason_code: str = "persistence_memory",
) -> SideEffectPersistenceBundle:
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
        approval_store=InMemoryApprovalStore(),
        permit_store=InMemoryExecutionPermitStore(),
        workflow_runtime_store=InMemoryWorkflowStateStore(),
        schedule_store=InMemoryScheduleStore(),
        task_queue_store=InMemoryTaskQueueStore(),
        protected_state_ready=False,
        reason_code=reason_code,
    )


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
        return _memory_bundle(encryption=encryption)
    try:
        path = _normalize_db_path(path_raw)
        connection = SqliteConnection(path)
        try:
            version = connection.initialize_schema()
        except Exception:
            try:
                connection.close()
            except Exception:
                pass
            raise
        exec_store = PersistentSideEffectExecutionStore(
            connection, encryption=encryption
        )
        idem_store = PersistentIdempotencyStore(connection, encryption=encryption)
        recon_store = PersistentReconciliationStore(connection, encryption=encryption)
        approval_store = PersistentApprovalStore(connection, encryption=encryption)
        permit_store = PersistentExecutionPermitStore(
            connection, encryption=encryption
        )
        workflow_store = PersistentWorkflowRuntimeStore(
            connection, encryption=encryption
        )
        from workflow.schedule_store import PersistentScheduleStore
        from task_queue.sqlite_store import PersistentTaskQueueStore

        schedule_store = PersistentScheduleStore(connection)
        task_queue_store = PersistentTaskQueueStore(connection)
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
            # Local-only expiry normalization (no approvals/permits issued, no network).
            approval_store.normalize_expired()
            permit_store.normalize_expired()
            scan = scan_recovery_candidates(
                execution_store=exec_store,
                idempotency_store=idem_store,
                reconciliation_store=recon_store,
                approval_store=approval_store,
                permit_store=permit_store,
                workflow_runtime_store=workflow_store,
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
            approval_store=approval_store,
            permit_store=permit_store,
            workflow_runtime_store=workflow_store,
            schedule_store=schedule_store,
            task_queue_store=task_queue_store,
            protected_state_ready=True,
            reason_code="persistence_ready",
            metadata={
                "schema_version": version,
                "recovery_scan": None if scan is None else scan.as_dict(),
                "protected_state_ready": True,
            },
        )
    except SideEffectPersistenceUnavailableError as exc:
        failed = _memory_bundle(
            encryption=encryption, reason_code=str(exc.error_code)
        )
        return SideEffectPersistenceBundle(
            backend="sqlite",
            ready=False,
            connection=None,
            execution_store=failed.execution_store,
            idempotency_store=failed.idempotency_store,
            reconciliation_store=failed.reconciliation_store,
            idempotency_registry=failed.idempotency_registry,
            encryption=encryption,
            schema_version=None,
            database_path_ref=None,
            approval_store=failed.approval_store,
            permit_store=failed.permit_store,
            workflow_runtime_store=failed.workflow_runtime_store,
            schedule_store=failed.schedule_store,
            task_queue_store=failed.task_queue_store,
            protected_state_ready=False,
            reason_code=str(exc.error_code),
        )
    except Exception:
        failed = _memory_bundle(
            encryption=encryption,
            reason_code="side_effect_persistence_unavailable",
        )
        return SideEffectPersistenceBundle(
            backend="sqlite",
            ready=False,
            connection=None,
            execution_store=failed.execution_store,
            idempotency_store=failed.idempotency_store,
            reconciliation_store=failed.reconciliation_store,
            idempotency_registry=failed.idempotency_registry,
            encryption=encryption,
            schema_version=None,
            database_path_ref=None,
            approval_store=failed.approval_store,
            permit_store=failed.permit_store,
            workflow_runtime_store=failed.workflow_runtime_store,
            schedule_store=failed.schedule_store,
            task_queue_store=failed.task_queue_store,
            protected_state_ready=False,
            reason_code="side_effect_persistence_unavailable",
        )


def attach_protected_persistence(engine, bundle: SideEffectPersistenceBundle, *, authority=None):
    """Compatibility helper: wire an external WorkflowEngine to a persistence bundle.

    Production composition uses compose_side_effect_runtime() / build_protected_services()
    and does not require this call. Idempotent when stores already match the bundle.
    """

    from hitl.authority import InMemoryApprovalAuthority, ROLE_PRIVILEGED_APPROVER
    from hitl.permit import PermitService
    from hitl.service import HITLService
    from workflow.state_manager import StateManager

    gate = engine._gate()
    if gate.approvals.store is bundle.approval_store and (
        engine.hitl_service is not None
        and getattr(engine.hitl_service, "store", None) is bundle.approval_store
        and getattr(getattr(engine.hitl_service, "permits", None), "store", None)
        is bundle.permit_store
        and engine.state_manager._store is bundle.workflow_runtime_store
    ):
        return engine

    gate.approvals.store = bundle.approval_store
    gate.idempotency = bundle.idempotency_registry
    engine.state_manager = StateManager(store=bundle.workflow_runtime_store)
    auth = authority
    if auth is None and engine.hitl_service is not None:
        auth = engine.hitl_service.authority
    if auth is None:
        auth = InMemoryApprovalAuthority()
        auth.grant("reviewer-1", ROLE_PRIVILEGED_APPROVER)
    engine.hitl_service = HITLService(
        gate=gate,
        state_manager=engine.state_manager,
        store=bundle.approval_store,
        authority=auth,
        permits=PermitService(store=bundle.permit_store),
        approval_ttl_seconds=3600,
        permit_ttl_seconds=300,
    )
    return engine
