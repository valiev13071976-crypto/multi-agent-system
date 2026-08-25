from dataclasses import dataclass, field
from types import MappingProxyType

from autonomy.models import sanitize_metadata
from security.encryption import EncryptionService
from security.secrets import EnvSecretStore, SecretStore
from side_effects.audit import SideEffectAuditLog
from side_effects.executor import SideEffectExecutor
from side_effects.factory import build_production_side_effect_registry
from side_effects.github.activation import GitHubWriteActivationService
from side_effects.github.config import GitHubWriteAdapterConfig, TOKEN_SECRET_NAME
from side_effects.github.errors import GitHubWriteConfigError
from side_effects.github.transport import GitHubHttpTransport
from side_effects.persistence import SideEffectPersistenceBundle, build_side_effect_persistence
from side_effects.reconciliation import SideEffectReconciliationService
from side_effects.registry import empty_adapter_registry


def _meta(value):
    return MappingProxyType(sanitize_metadata(value))


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
        """Optional read-only readiness probe. Never mutates. Never dry-runs an action."""

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
) -> SideEffectRuntime:
    """Build registry + executor + activation. Does not call GitHub unless probe is run later.

    isolate_errors=True keeps /api/analyze available if GitHub config is invalid.
    Fake transport is never the production default; pass transport only in tests.
    """

    audit = audit or SideEffectAuditLog()
    secrets = secrets or EnvSecretStore()
    if persistence is None:
        if encryption is None:
            try:
                encryption = EncryptionService.from_env()
            except Exception:
                encryption = None
        # Default remains memory unless SIDE_EFFECT_PERSISTENCE_BACKEND/path requests sqlite.
        persistence = build_side_effect_persistence(
            env=env,
            encryption=encryption,
            durable=durable_persistence,
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
        executor = SideEffectExecutor(
            registry,
            audit=audit,
            activation=activation,
            store=persistence.execution_store,
            idempotency=persistence.idempotency_registry,
            persistence=persistence,
        )
        return SideEffectRuntime(
            config=disabled,
            registry=registry,
            executor=executor,
            activation=activation,
            audit=audit,
            composition_error=exc.error_code,
            persistence=persistence,
            recovery_scan=persistence.last_scan,
        )
    if not config.enabled:
        activation = GitHubWriteActivationService(
            config=config,
            audit=audit,
            registered=False,
            persistence_ready=True,
        )
        registry = empty_adapter_registry()
        executor = SideEffectExecutor(
            registry,
            audit=audit,
            activation=activation,
            store=persistence.execution_store,
            idempotency=persistence.idempotency_registry,
            persistence=persistence,
        )
        return SideEffectRuntime(
            config=config,
            registry=registry,
            executor=executor,
            activation=activation,
            audit=audit,
            persistence=persistence,
            recovery_scan=persistence.last_scan,
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
        executor = SideEffectExecutor(
            registry,
            audit=audit,
            activation=activation,
            store=persistence.execution_store,
            idempotency=persistence.idempotency_registry,
            persistence=persistence,
        )
        return SideEffectRuntime(
            config=config,
            registry=registry,
            executor=executor,
            activation=activation,
            audit=audit,
            composition_error=exc.error_code,
            persistence=persistence,
            recovery_scan=persistence.last_scan,
        )
    require_durable = bool(config.enabled and not config.dry_run)
    persistence_ready = True
    if require_durable:
        # Real mutate mode requires durable persistence when sqlite backend requested,
        # or when explicitly marked durable.
        if persistence.backend == "sqlite":
            persistence_ready = bool(persistence.ready)
        elif durable_persistence:
            persistence_ready = bool(persistence.ready)
    activation = GitHubWriteActivationService(
        config=config,
        transport=resolved,
        audit=audit,
        registered=True,
        persistence_ready=persistence_ready,
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
        persistence=persistence,
        require_durable_persistence=require_durable and persistence.backend == "sqlite",
    )
    return SideEffectRuntime(
        config=config,
        registry=registry,
        executor=executor,
        activation=activation,
        audit=audit,
        persistence=persistence,
        recovery_scan=persistence.last_scan,
    )
