"""Production composition owner for side-effect + protected-state runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

from autonomy.approval import ApprovalService
from autonomy.gate import AutonomyGate
from autonomy.models import sanitize_metadata
from hitl.authority import InMemoryApprovalAuthority, ROLE_PRIVILEGED_APPROVER
from hitl.permit import PermitService
from hitl.service import HITLService
from security.encryption import EncryptionService
from security.secrets import EnvSecretStore, SecretStore
from side_effects.audit import SideEffectAuditLog
from side_effects.executor import SideEffectExecutor
from side_effects.factory import build_production_side_effect_registry
from side_effects.github.activation import GitHubWriteActivationService
from side_effects.github.config import GitHubWriteAdapterConfig, TOKEN_SECRET_NAME
from side_effects.github.errors import GitHubWriteConfigError
from side_effects.github.transport import GitHubHttpTransport
from side_effects.persistence import (
    SideEffectPersistenceBundle,
    attach_protected_persistence,
    build_side_effect_persistence,
)
from side_effects.reconciliation import SideEffectReconciliationService
from side_effects.registry import empty_adapter_registry
from tools.adapters import descriptor_from_side_effect, github_issue_labels_descriptor
from tools.gateway import ToolGateway
from tools.registry import ToolRegistry
from observability.runtime import ObservabilityRuntime, build_observability_runtime
from workflow.engine import WorkflowEngine
from workflow.state_manager import StateManager


def _meta(value):
    return MappingProxyType(sanitize_metadata(value))


def _store_backend_label(store) -> str:
    name = type(store).__name__
    if name.startswith("Persistent"):
        return "sqlite"
    return "memory"


def build_protected_services(
    persistence: SideEffectPersistenceBundle,
    *,
    authority=None,
):
    """Explicit DI: gate / HITL / permit / workflow from one persistence bundle."""

    gate = AutonomyGate(
        approvals=ApprovalService(store=persistence.approval_store),
        idempotency=persistence.idempotency_registry,
    )
    state_manager = StateManager(store=persistence.workflow_runtime_store)
    auth = authority
    if auth is None:
        auth = InMemoryApprovalAuthority()
        auth.grant("reviewer-1", ROLE_PRIVILEGED_APPROVER)
    permits = PermitService(store=persistence.permit_store)
    hitl = HITLService(
        gate=gate,
        state_manager=state_manager,
        store=persistence.approval_store,
        authority=auth,
        permits=permits,
        approval_ttl_seconds=3600,
        permit_ttl_seconds=300,
    )
    workflow_engine = WorkflowEngine(
        state_manager=state_manager,
        autonomy_gate=gate,
        hitl_service=hitl,
    )
    attached = bool(
        persistence.backend == "sqlite"
        and persistence.ready
        and persistence.protected_state_ready
    )
    return {
        "gate": gate,
        "hitl_service": hitl,
        "permit_service": permits,
        "workflow_engine": workflow_engine,
        "protected_persistence_attached": attached,
    }


def _real_write_persistence_gate(
    persistence: SideEffectPersistenceBundle,
    *,
    require_durable: bool,
    durable_persistence: bool | None,
) -> tuple[bool, str]:
    """Return (persistence_ready, unavailable_reason) for real mutate mode."""

    if not require_durable:
        return True, "side_effect_persistence_unavailable"
    if persistence.backend == "sqlite":
        if not persistence.ready:
            reason = persistence.reason_code or "side_effect_persistence_unavailable"
            if reason in {
                "protected_state_persistence_unavailable",
                "side_effect_schema_version_unsupported",
            }:
                return False, reason
            return False, reason
        if not persistence.protected_state_ready:
            return False, "protected_state_persistence_unavailable"
        return True, "side_effect_persistence_unavailable"
    if durable_persistence:
        if not (persistence.ready and persistence.protected_state_ready):
            return False, "protected_state_persistence_unavailable"
        return True, "side_effect_persistence_unavailable"
    # Real write with memory backend is not a silent sqlite→memory fallback path;
    # activation for GitHub real mode still requires durable sqlite when configured.
    return True, "side_effect_persistence_unavailable"


@dataclass
class SideEffectRuntime:
    """Composed production side-effect stack. Analyze does not invoke writes."""

    config: GitHubWriteAdapterConfig
    registry: object
    executor: SideEffectExecutor
    activation: GitHubWriteActivationService
    audit: SideEffectAuditLog
    composition_error: str | None = None
    startup_probe_ran: bool = False
    persistence: SideEffectPersistenceBundle | None = None
    recovery_scan: object | None = None
    workflow_engine: WorkflowEngine | None = None
    hitl_service: HITLService | None = None
    autonomy_gate: AutonomyGate | None = None
    permit_service: PermitService | None = None
    protected_persistence_attached: bool = False
    tool_registry: ToolRegistry | None = None
    tool_gateway: ToolGateway | None = None
    observability: ObservabilityRuntime | None = None
    recovery_orchestrator: object | None = None
    memory_runtime: object | None = None
    document_runtime: object | None = None
    knowledge_runtime: object | None = None
    procurement_runtime: object | None = None
    _start_completed: bool = field(default=False, repr=False)

    def health(self):
        health = self.activation.health()
        meta = dict(health.metadata)
        if self.persistence is not None:
            meta.update(
                {
                    "persistence_backend": self.persistence.backend,
                    "persistence_ready": self.persistence.ready,
                    "database_path_ref": self.persistence.database_path_ref,
                    "schema_version": self.persistence.schema_version,
                    "protected_state_ready": self.persistence.protected_state_ready,
                    "protected_persistence_attached": self.protected_persistence_attached,
                    "workflow_store_backend": _store_backend_label(
                        self.persistence.workflow_runtime_store
                    ),
                    "approval_store_backend": _store_backend_label(
                        self.persistence.approval_store
                    ),
                    "permit_store_backend": _store_backend_label(
                        self.persistence.permit_store
                    ),
                }
            )
            if self.persistence.last_scan is not None:
                scan = self.persistence.last_scan.as_dict()
                meta["recovery_scan"] = scan
                meta["pending_approval_count"] = scan.get("pending_approval_count", 0)
                meta["expired_approval_count"] = scan.get("expired_approval_count", 0)
                meta["active_permit_count"] = scan.get("active_permit_count", 0)
                meta["expired_permit_count"] = scan.get("expired_permit_count", 0)
                meta["waiting_approval_workflow_count"] = scan.get(
                    "waiting_approval_workflow_count", 0
                )
        else:
            meta["protected_persistence_attached"] = False
        if self.tool_registry is not None:
            meta["tool_gateway"] = dict(self.tool_registry.health())
        if self.observability is not None:
            open_recovery = 0
            manual = 0
            stale_jobs = 0
            blocked_crit = False
            recovery_ready = True
            if self.recovery_orchestrator is not None:
                open_cases = self.recovery_orchestrator.list_open_cases()
                open_recovery = len(open_cases)
                manual = sum(1 for c in open_cases if c.status == "waiting_operator")
                if hasattr(self.recovery_orchestrator, "get_due_jobs"):
                    stale_jobs = len(self.recovery_orchestrator.get_due_jobs())
                blocked_crit = bool(
                    getattr(self.recovery_orchestrator, "mutation_blocked_reason", None)
                )
                store = getattr(self.recovery_orchestrator, "store", None)
                if store is not None:
                    recovery_ready = bool(getattr(store, "available", True))
            snap = self.observability.health(
                persistence_ready=bool(
                    self.persistence is not None and self.persistence.ready
                ),
                protected_state_ready=bool(
                    self.persistence is not None and self.persistence.protected_state_ready
                ),
                protected_write_required=bool(
                    self.config.enabled and not self.config.dry_run
                ),
                open_recovery_cases=open_recovery,
                pending_manual_review=manual,
                stale_recovery_jobs=stale_jobs,
                critical_recovery_blocking=blocked_crit,
                recovery_persistence_ready=recovery_ready,
                recovery_required=bool(self.config.enabled and not self.config.dry_run),
                memory_status=(
                    (self.memory_runtime.health().get("memory_status") or "healthy")
                    if self.memory_runtime is not None
                    else "healthy"
                ),
                memory_enabled=bool(self.memory_runtime is not None),
                memory_persistence_ready=bool(
                    self.memory_runtime is None
                    or self.memory_runtime.health().get("persistence_ready", True)
                ),
                document_status=(
                    (self.document_runtime.health().get("document_status") or "healthy")
                    if self.document_runtime is not None
                    else "healthy"
                ),
                documents_enabled=bool(self.document_runtime is not None),
                document_persistence_ready=bool(
                    self.document_runtime is None
                    or self.document_runtime.health().get("persistence_ready", True)
                ),
                knowledge_status=(
                    (self.knowledge_runtime.health().get("knowledge_status") or "healthy")
                    if self.knowledge_runtime is not None
                    else "healthy"
                ),
                knowledge_enabled=bool(self.knowledge_runtime is not None),
                knowledge_persistence_ready=bool(
                    self.knowledge_runtime is None
                    or self.knowledge_runtime.health().get("persistence_ready", True)
                ),
                procurement_status=(
                    (self.procurement_runtime.health().get("procurement_status") or "healthy")
                    if self.procurement_runtime is not None
                    else "healthy"
                ),
                procurement_enabled=bool(self.procurement_runtime is not None),
                procurement_persistence_ready=bool(
                    self.procurement_runtime is None
                    or self.procurement_runtime.health().get("persistence_ready", True)
                ),
            )
            meta["observability"] = {
                "overall_status": snap.overall_status,
                "uncertain_side_effects": snap.uncertain_side_effects,
                "tool_failures_recent": snap.tool_failures_recent,
            }
        if self.recovery_orchestrator is not None:
            open_cases = len(self.recovery_orchestrator.list_open_cases())
            blocked = getattr(self.recovery_orchestrator, "mutation_blocked_reason", None)
            store = getattr(self.recovery_orchestrator, "store", None)
            meta["recovery"] = {
                "open_cases": open_cases,
                "mutation_blocked_reason": blocked,
                "enabled": bool(getattr(self.recovery_orchestrator, "enabled", True)),
                "persistence_backend": getattr(store, "persistence_backend", "unknown"),
                "persistence_ready": bool(getattr(store, "available", False)),
                "connection_mode": getattr(store, "connection_mode", "unknown"),
            }
        if self.memory_runtime is not None:
            meta["memory"] = dict(self.memory_runtime.health())
        if self.document_runtime is not None:
            meta["documents"] = dict(self.document_runtime.health())
        if self.knowledge_runtime is not None:
            meta["knowledge"] = dict(self.knowledge_runtime.health())
        if self.procurement_runtime is not None:
            meta["procurement"] = dict(self.procurement_runtime.health())
        return type(health)(
            adapter_id=health.adapter_id,
            activation_state=health.activation_state,
            configured=health.configured,
            registered=health.registered,
            dry_run=health.dry_run,
            kill_switch=health.kill_switch,
            readiness_status=health.readiness_status,
            last_probe_at=health.last_probe_at,
            reason_code=health.reason_code,
            metadata=meta,
        )

    async def start(self):
        """Optional read-only readiness probe. Idempotent. Never mutates."""

        if self._start_completed:
            return self.activation.readiness
        self._start_completed = True
        if self.composition_error or not self.config.enabled:
            return self.activation.readiness
        if not self.config.probe_on_startup:
            return self.activation.readiness
        try:
            result = await self.activation.refresh()
        except Exception:
            self.startup_probe_ran = True
            return self.activation.readiness
        self.startup_probe_ran = True
        return result

    def close(self) -> None:
        """Release owned resources. Shared recovery store does not close P7 connection."""

        orch = self.recovery_orchestrator
        if orch is not None:
            store = getattr(orch, "store", None)
            if store is not None and hasattr(store, "close"):
                try:
                    store.close()
                except Exception:
                    pass
        if self.persistence is not None and self.persistence.connection is not None:
            try:
                self.persistence.connection.close()
            except Exception:
                pass
        if self.memory_runtime is not None and hasattr(self.memory_runtime, "close"):
            try:
                self.memory_runtime.close()
            except Exception:
                pass
        if self.document_runtime is not None and hasattr(self.document_runtime, "close"):
            try:
                self.document_runtime.close()
            except Exception:
                pass
        if self.knowledge_runtime is not None and hasattr(self.knowledge_runtime, "close"):
            try:
                self.knowledge_runtime.close()
            except Exception:
                pass
        if self.procurement_runtime is not None and hasattr(self.procurement_runtime, "close"):
            try:
                self.procurement_runtime.close()
            except Exception:
                pass


def build_tool_gateway(
    *,
    side_effect_registry,
    executor,
    gate,
    hitl,
    github_enabled: bool = False,
    observability: ObservabilityRuntime | None = None,
) -> tuple[ToolRegistry, ToolGateway]:
    """Register built-in tools, optionally GitHub write tool, then freeze."""

    tool_registry = ToolRegistry()
    gateway = ToolGateway(
        registry=tool_registry,
        side_effect_executor=executor,
        gate=gate,
        hitl=hitl,
        register_search=True,
        observability=observability,
    )
    github_adapter = None
    if hasattr(side_effect_registry, "get"):
        github_adapter = side_effect_registry.get("github.issue_labels")
    if github_adapter is not None:
        se_desc = getattr(github_adapter, "descriptor", None)
        if se_desc is not None:
            tool_registry.register(
                descriptor_from_side_effect(
                    se_desc,
                    name="GitHub Issue Labels",
                    description="Bounded reversible GitHub issue label mutations",
                    version="1.0.0",
                    enabled=True,
                    idempotency_required=True,
                    timeout_seconds=15.0,
                ),
                adapter=github_adapter,
            )
        else:
            tool_registry.register(
                github_issue_labels_descriptor(enabled=True),
                adapter=github_adapter,
            )
    else:
        tool_registry.register(
            github_issue_labels_descriptor(enabled=False), adapter=None
        )
    tool_registry.freeze()
    gateway.side_effect_executor = executor
    gateway.gate = gate
    gateway.hitl = hitl
    gateway.observability = observability
    _ = github_enabled
    return tool_registry, gateway


def _finalize_runtime(
    *,
    config,
    registry,
    executor,
    activation,
    audit,
    persistence: SideEffectPersistenceBundle,
    composition_error: str | None = None,
    services: dict,
    observability: ObservabilityRuntime | None = None,
    env: dict | None = None,
) -> SideEffectRuntime:
    obs = observability or build_observability_runtime(env={})
    engine = services["workflow_engine"]
    engine.side_effect_executor = executor
    engine.observability = obs
    gate = services["gate"]
    gate.observability = obs
    hitl = services["hitl_service"]
    hitl.observability = obs
    permit = services["permit_service"]
    permit.observability = obs
    executor.observability = obs
    if getattr(executor, "reconciliation_service", None) is not None:
        executor.reconciliation_service.observability = obs
    tool_registry, tool_gateway = build_tool_gateway(
        side_effect_registry=registry,
        executor=executor,
        gate=gate,
        hitl=hitl,
        github_enabled=bool(config.enabled and registry.get("github.issue_labels") is not None)
        if hasattr(registry, "get")
        else False,
        observability=obs,
    )
    recovery = None
    from recovery.runtime import (
        _fail_closed_orchestrator,
        build_recovery_orchestrator,
        recovery_config,
        run_startup_recovery_materialization,
    )
    from recovery.store import RecoveryPersistenceUnavailableError

    try:
        recovery = build_recovery_orchestrator(
            env=env,
            persistence=persistence,
            reconciliation_service=getattr(executor, "reconciliation_service", None),
            workflow_engine=engine,
            gate=gate,
            hitl=hitl,
            side_effect_executor=executor,
            observability=obs,
            audit=audit,
        )
    except RecoveryPersistenceUnavailableError:
        recovery = _fail_closed_orchestrator(
            recovery_config(env),
            reconciliation_service=getattr(executor, "reconciliation_service", None),
            workflow_engine=engine,
            gate=gate,
            hitl=hitl,
            side_effect_executor=executor,
            observability=obs,
            audit=audit,
        )
    except Exception:
        # Non-persistence composition errors: fail closed only for durable sqlite.
        if (
            persistence is not None
            and persistence.backend == "sqlite"
            and persistence.ready
            and persistence.connection is not None
        ):
            recovery = _fail_closed_orchestrator(
                recovery_config(env),
                reconciliation_service=getattr(executor, "reconciliation_service", None),
                workflow_engine=engine,
                gate=gate,
                hitl=hitl,
                side_effect_executor=executor,
                observability=obs,
                audit=audit,
            )
        else:
            recovery = None
    # Attach recovery when enabled.
    if recovery is not None:
        executor.recovery_orchestrator = recovery
        if (
            persistence is not None
            and persistence.last_scan is not None
            and getattr(recovery.store, "available", True)
        ):
            try:
                run_startup_recovery_materialization(
                    recovery,
                    execution_store=persistence.execution_store,
                    reconciliation_store=persistence.reconciliation_store,
                    permit_store=persistence.permit_store,
                    enqueue=True,
                )
            except Exception:
                pass

    memory_runtime = None
    from memory.runtime import build_memory_runtime, memory_config

    mem_cfg = memory_config(env)
    shared_mem = None
    if mem_cfg["backend"] in {"sqlite", "durable"} and not mem_cfg["db_path"]:
        if (
            persistence is not None
            and persistence.backend == "sqlite"
            and persistence.connection is not None
            and persistence.ready
        ):
            shared_mem = persistence.connection
    try:
        memory_runtime = build_memory_runtime(
            env=env,
            encryption=getattr(persistence, "encryption", None),
            observability=obs,
            shared_connection=shared_mem,
        )
    except Exception:
        memory_runtime = None
    if memory_runtime is not None:
        engine.memory_service = memory_runtime.service

    document_runtime = None
    from documents.runtime import build_document_runtime, document_config

    doc_cfg = document_config(env)
    shared_doc = None
    if doc_cfg["backend"] in {"sqlite", "durable"} and not doc_cfg["db_path"]:
        if (
            persistence is not None
            and persistence.backend == "sqlite"
            and persistence.connection is not None
            and persistence.ready
        ):
            shared_doc = persistence.connection
    try:
        document_runtime = build_document_runtime(
            env=env,
            encryption=getattr(persistence, "encryption", None),
            observability=obs,
            memory_service=memory_runtime.service if memory_runtime else None,
            shared_connection=shared_doc,
        )
    except Exception:
        document_runtime = None
    if document_runtime is not None:
        engine.document_service = document_runtime.service

    knowledge_runtime = None
    from knowledge.runtime import build_knowledge_runtime

    try:
        knowledge_runtime = build_knowledge_runtime(
            env=env,
            memory_service=memory_runtime.service if memory_runtime else None,
            document_service=document_runtime.service if document_runtime else None,
            tool_gateway=tool_gateway,
            observability=obs,
            freeze=True,
        )
    except Exception:
        knowledge_runtime = None
    if knowledge_runtime is not None:
        engine.knowledge_service = knowledge_runtime.service

    procurement_runtime = None
    from procurement.runtime import build_procurement_runtime, procurement_config

    proc_cfg = procurement_config(env)
    shared_proc = None
    if proc_cfg["backend"] in {"sqlite", "durable"} and not proc_cfg["db_path"]:
        if (
            persistence is not None
            and persistence.backend == "sqlite"
            and persistence.connection is not None
            and persistence.ready
        ):
            shared_proc = persistence.connection
    try:
        procurement_runtime = build_procurement_runtime(
            env=env,
            knowledge_service=knowledge_runtime.service if knowledge_runtime else None,
            document_service=document_runtime.service if document_runtime else None,
            memory_service=memory_runtime.service if memory_runtime else None,
            workflow_engine=engine,
            tool_gateway=tool_gateway,
            autonomy_gate=gate,
            hitl_service=hitl,
            observability=obs,
            shared_connection=shared_proc,
            encryption=getattr(persistence, "encryption", None),
        )
    except Exception:
        procurement_runtime = None
    if procurement_runtime is not None:
        engine.procurement_service = procurement_runtime.service

    return SideEffectRuntime(
        config=config,
        registry=registry,
        executor=executor,
        activation=activation,
        audit=audit,
        composition_error=composition_error,
        persistence=persistence,
        recovery_scan=persistence.last_scan,
        workflow_engine=engine,
        hitl_service=hitl,
        autonomy_gate=gate,
        permit_service=permit,
        protected_persistence_attached=services["protected_persistence_attached"],
        tool_registry=tool_registry,
        tool_gateway=tool_gateway,
        observability=obs,
        recovery_orchestrator=recovery,
        memory_runtime=memory_runtime,
        document_runtime=document_runtime,
        knowledge_runtime=knowledge_runtime,
        procurement_runtime=procurement_runtime,
    )


def compose_side_effect_runtime(
    *,
    secrets: SecretStore | None = None,
    env: dict | None = None,
    transport=None,
    isolate_errors: bool = True,
    audit: SideEffectAuditLog | None = None,
    persistence: SideEffectPersistenceBundle | None = None,
    encryption: EncryptionService | None = None,
    durable_persistence: bool | None = None,
    authority=None,
) -> SideEffectRuntime:
    """Build registry + executor + activation + HITL/workflow DI.

    Does not call GitHub unless probe is run later via runtime.start().
    isolate_errors=True keeps /api/analyze available if GitHub config is invalid.
    """

    audit = audit or SideEffectAuditLog()
    secrets = secrets or EnvSecretStore()
    observability = build_observability_runtime(env=env)
    if persistence is None:
        if encryption is None:
            try:
                encryption = EncryptionService.from_env()
            except Exception:
                encryption = None
        persistence = build_side_effect_persistence(
            env=env,
            encryption=encryption,
            durable=durable_persistence,
        )
    services = build_protected_services(persistence, authority=authority)

    def _executor(activation, registry, *, require_durable: bool = False):
        return SideEffectExecutor(
            registry,
            audit=audit,
            activation=activation,
            store=persistence.execution_store,
            idempotency=persistence.idempotency_registry,
            gate=services["gate"],
            permit_service=services["permit_service"],
            persistence=persistence,
            require_durable_persistence=require_durable
            and persistence.backend == "sqlite",
        )

    try:
        config = GitHubWriteAdapterConfig.from_env(env)
    except GitHubWriteConfigError as exc:
        if not isolate_errors:
            raise
        disabled = GitHubWriteAdapterConfig()
        activation = GitHubWriteActivationService(
            config=disabled,
            audit=audit,
            registered=False,
            composition_error=exc.error_code,
            persistence_ready=True,
        )
        registry = empty_adapter_registry()
        executor = _executor(activation, registry)
        return _finalize_runtime(
            config=disabled,
            registry=registry,
            executor=executor,
            activation=activation,
            audit=audit,
            persistence=persistence,
            composition_error=exc.error_code,
            services=services,
            observability=observability,
            env=env,
        )
    if not config.enabled:
        activation = GitHubWriteActivationService(
            config=config,
            audit=audit,
            registered=False,
            persistence_ready=True,
        )
        registry = empty_adapter_registry()
        executor = _executor(activation, registry)
        return _finalize_runtime(
            config=config,
            registry=registry,
            executor=executor,
            activation=activation,
            audit=audit,
            persistence=persistence,
            services=services,
            observability=observability,
            env=env,
        )
    try:
        if not config.allowed_repositories:
            raise GitHubWriteConfigError("github_allowlist_empty")
        token = secrets.get(TOKEN_SECRET_NAME)
        if token is None or not str(token).strip():
            raise GitHubWriteConfigError("github_write_secret_missing")
        resolved = transport
        if resolved is None:
            resolved = GitHubHttpTransport(token, timeout_seconds=config.timeout_seconds)
        registry = build_production_side_effect_registry(
            secrets=secrets, config=config, transport=resolved
        )
    except GitHubWriteConfigError as exc:
        if not isolate_errors:
            raise
        activation = GitHubWriteActivationService(
            config=config,
            audit=audit,
            registered=False,
            composition_error=exc.error_code,
            persistence_ready=persistence.ready,
        )
        registry = empty_adapter_registry()
        executor = _executor(activation, registry)
        return _finalize_runtime(
            config=config,
            registry=registry,
            executor=executor,
            activation=activation,
            audit=audit,
            persistence=persistence,
            composition_error=exc.error_code,
            services=services,
            observability=observability,
            env=env,
        )
    require_durable = bool(config.enabled and not config.dry_run)
    persistence_ready, unavailable_reason = _real_write_persistence_gate(
        persistence,
        require_durable=require_durable,
        durable_persistence=durable_persistence,
    )
    activation = GitHubWriteActivationService(
        config=config,
        transport=resolved,
        audit=audit,
        registered=True,
        persistence_ready=persistence_ready,
        persistence_unavailable_reason=unavailable_reason,
    )
    recon = SideEffectReconciliationService(
        execution_store=persistence.execution_store,
        idempotency=persistence.idempotency_registry,
        registry=registry,
        audit=audit,
        store=persistence.reconciliation_store,
    )
    recon.observability = observability
    executor = SideEffectExecutor(
        registry,
        audit=audit,
        activation=activation,
        store=persistence.execution_store,
        idempotency=persistence.idempotency_registry,
        reconciliation_service=recon,
        gate=services["gate"],
        permit_service=services["permit_service"],
        persistence=persistence,
        require_durable_persistence=require_durable and persistence.backend == "sqlite",
    )
    return _finalize_runtime(
        config=config,
        registry=registry,
        executor=executor,
        activation=activation,
        audit=audit,
        persistence=persistence,
        services=services,
        observability=observability,
        env=env,
    )


# Compatibility re-export for callers/tests that still import attach from runtime.
__all__ = [
    "SideEffectRuntime",
    "compose_side_effect_runtime",
    "build_protected_services",
    "attach_protected_persistence",
]
