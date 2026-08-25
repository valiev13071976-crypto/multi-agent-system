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
) -> SideEffectRuntime:
    engine = services["workflow_engine"]
    engine.side_effect_executor = executor
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
        hitl_service=services["hitl_service"],
        autonomy_gate=services["gate"],
        permit_service=services["permit_service"],
        protected_persistence_attached=services["protected_persistence_attached"],
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
    )


# Compatibility re-export for callers/tests that still import attach from runtime.
__all__ = [
    "SideEffectRuntime",
    "compose_side_effect_runtime",
    "build_protected_services",
    "attach_protected_persistence",
]
