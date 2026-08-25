from dataclasses import dataclass

from security.secrets import EnvSecretStore, SecretStore
from side_effects.audit import SideEffectAuditLog
from side_effects.executor import SideEffectExecutor
from side_effects.factory import build_production_side_effect_registry
from side_effects.github.activation import GitHubWriteActivationService
from side_effects.github.config import GitHubWriteAdapterConfig, TOKEN_SECRET_NAME
from side_effects.github.errors import GitHubWriteConfigError
from side_effects.github.transport import GitHubHttpTransport
from side_effects.registry import empty_adapter_registry


@dataclass
class SideEffectRuntime:
    """Composed production side-effect stack. Analyze does not invoke writes."""

    config: GitHubWriteAdapterConfig
    registry: object
    executor: SideEffectExecutor
    activation: GitHubWriteActivationService
    audit: SideEffectAuditLog
    composition_error: str | None = None

    def health(self):
        return self.activation.health()


def compose_side_effect_runtime(
    *,
    secrets: SecretStore | None = None,
    env: dict | None = None,
    transport=None,
    isolate_errors: bool = True,
    audit: SideEffectAuditLog | None = None,
) -> SideEffectRuntime:
    """Build registry + executor + activation. Does not call GitHub unless probe is run later.

    isolate_errors=True keeps /api/analyze available if GitHub config is invalid.
    Fake transport is never the production default; pass transport only in tests.
    """

    audit = audit or SideEffectAuditLog()
    secrets = secrets or EnvSecretStore()
    try:
        config = GitHubWriteAdapterConfig.from_env(env)
    except GitHubWriteConfigError as exc:
        if not isolate_errors:
            raise
        disabled = GitHubWriteAdapterConfig()
        activation = GitHubWriteActivationService(
            config=disabled, audit=audit, registered=False, composition_error=exc.error_code
        )
        registry = empty_adapter_registry()
        executor = SideEffectExecutor(registry, audit=audit, activation=activation)
        return SideEffectRuntime(
            config=disabled,
            registry=registry,
            executor=executor,
            activation=activation,
            audit=audit,
            composition_error=exc.error_code,
        )
    if not config.enabled:
        activation = GitHubWriteActivationService(
            config=config, audit=audit, registered=False
        )
        registry = empty_adapter_registry()
        executor = SideEffectExecutor(registry, audit=audit, activation=activation)
        return SideEffectRuntime(
            config=config,
            registry=registry,
            executor=executor,
            activation=activation,
            audit=audit,
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
            config=config, audit=audit, registered=False, composition_error=exc.error_code
        )
        registry = empty_adapter_registry()
        executor = SideEffectExecutor(registry, audit=audit, activation=activation)
        return SideEffectRuntime(
            config=config,
            registry=registry,
            executor=executor,
            activation=activation,
            audit=audit,
            composition_error=exc.error_code,
        )
    activation = GitHubWriteActivationService(
        config=config,
        transport=resolved,
        audit=audit,
        registered=True,
    )
    executor = SideEffectExecutor(registry, audit=audit, activation=activation)
    return SideEffectRuntime(
        config=config,
        registry=registry,
        executor=executor,
        activation=activation,
        audit=audit,
    )
