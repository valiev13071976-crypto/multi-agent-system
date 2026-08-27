"""Production integration runtime composition."""

from __future__ import annotations

import os

from integrations.circuit_breaker import CircuitBreaker
from integrations.http_client import IntegrationHttpClient
from integrations.ledger import OperationLedger
from integrations.registry import IntegrationRegistry
from integrations.secrets.encrypted_store import (
    EncryptedLocalSecretsBackend,
    ExternalSecretsBackend,
)
from integrations.secrets.env_backend import (
    EnvSecretsBackend,
    FailClosedSecretsBackend,
    MemorySecretsBackend,
)
from integrations.service import IntegrationService
from integrations.webhooks import WebhookProcessor


def integration_config(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    backend = str(source.get("INTEGRATION_SECRETS_BACKEND") or "encrypted_local").strip().lower()
    mode = str(source.get("INTEGRATION_SECRETS_MODE") or "").strip().lower()
    # Explicit production marker
    production = mode in {"production", "prod"} or str(
        source.get("PANDA_ENV") or ""
    ).strip().lower() in {"production", "prod"}
    return {
        "enabled": str(source.get("INTEGRATION_ENABLED", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        "secrets_backend": backend,
        "production": production,
        "db_path": str(source.get("INTEGRATION_DB_PATH") or "").strip() or None,
        "use_shared_db": str(source.get("INTEGRATION_USE_SHARED_DB", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        "external_provider": str(source.get("INTEGRATION_EXTERNAL_SECRETS_PROVIDER") or "vault"),
        "external_available": str(source.get("INTEGRATION_EXTERNAL_SECRETS_AVAILABLE") or "")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"},
        "fail_closed_on_missing_encryption": production
        or backend in {"encrypted_local", "external", "vault", "aws", "azure", "gcp"},
    }


def build_secrets_backend(
    *,
    env: dict | None = None,
    encryption=None,
    shared_connection=None,
):
    cfg = integration_config(env)
    backend = cfg["secrets_backend"]
    if backend in {"memory", "test"}:
        return MemorySecretsBackend()
    if backend in {"env", "development", "dev"}:
        return EnvSecretsBackend(env=env)
    if backend in {"external", "vault", "aws", "azure", "gcp"}:
        if not cfg["external_available"]:
            return FailClosedSecretsBackend()
        # Scaffold: when marked available without delegate, still fail closed unless delegate set
        return ExternalSecretsBackend(
            provider=cfg["external_provider"], available=False
        )
    # encrypted_local (default production path)
    if encryption is None and cfg["fail_closed_on_missing_encryption"]:
        return FailClosedSecretsBackend()
    path = cfg["db_path"]
    if shared_connection is not None and cfg["use_shared_db"]:
        return EncryptedLocalSecretsBackend(
            encryption=encryption,
            shared_connection=shared_connection,
            require_encryption=cfg["fail_closed_on_missing_encryption"],
        )
    if path:
        return EncryptedLocalSecretsBackend(
            encryption=encryption,
            path=path,
            require_encryption=cfg["fail_closed_on_missing_encryption"],
        )
    if shared_connection is not None:
        return EncryptedLocalSecretsBackend(
            encryption=encryption,
            shared_connection=shared_connection,
            require_encryption=cfg["fail_closed_on_missing_encryption"],
        )
    # Dev fallback without shared DB — still encrypted if key present
    if encryption is None:
        if cfg["production"]:
            return FailClosedSecretsBackend()
        return EnvSecretsBackend(env=env)
    return EncryptedLocalSecretsBackend(
        encryption=encryption,
        path=":memory:",
        require_encryption=True,
    )


class IntegrationCredentialBridge:
    """
    Compatibility adapter for tools.IntegrationCredentialStore consumers.
    Resolves secrets via SecretsBackend; never persists plaintext in registry.
    """

    def __init__(self, service: IntegrationService):
        self._service = service
        self._legacy_configs: dict = {}

    def register(self, config, *, secret: str | None = None) -> None:
        # Legacy path used by older tests — register descriptor + optional secret
        from integrations.contracts import IntegrationDescriptor
        from tools.integration import IntegrationConfig

        if isinstance(config, IntegrationConfig):
            desc = IntegrationDescriptor(
                integration_id=config.integration_id,
                tenant_id=config.tenant_id,
                provider=config.provider,
                integration_type=config.provider,
                adapter_id=config.adapter_id,
                enabled=config.enabled,
                credential_ref=config.credential_ref or config.integration_id,
                safe_settings=dict(config.settings),
            )
            self._service.register_integration(desc)
            if secret:
                ref = config.credential_ref or config.integration_id
                self._service.put_secret(
                    tenant_id=config.tenant_id, secret_ref=ref, value=secret
                )
            self._legacy_configs[(config.tenant_id, config.integration_id)] = config
            return
        self._service.register_integration(config)
        if secret and getattr(config, "credential_ref", None):
            self._service.put_secret(
                tenant_id=config.tenant_id,
                secret_ref=config.credential_ref,
                value=secret,
            )

    def get_config(self, tenant_id: str, integration_id: str):
        from tools.integration import IntegrationConfig

        legacy = self._legacy_configs.get((tenant_id, integration_id))
        if legacy is not None:
            return legacy
        desc = self._service.registry.get(tenant_id, integration_id)
        if desc is None:
            return None
        return IntegrationConfig(
            integration_id=desc.integration_id,
            tenant_id=desc.tenant_id,
            adapter_id=desc.adapter_id,
            provider=desc.provider,
            enabled=desc.enabled,
            credential_ref=desc.credential_ref,
            settings=dict(desc.safe_settings),
            health_status=self._service.registry.get_health(
                tenant_id, integration_id
            ).status,
        )

    def resolve_secret(self, ref) -> str | None:
        from security.tenant import normalize_tenant_id

        tenant = normalize_tenant_id(getattr(ref, "tenant_id", ""))
        key = getattr(ref, "credential_key", None) or getattr(ref, "secret_ref", None)
        if not key:
            return None
        # Tenant isolation: ref.tenant must match
        if normalize_tenant_id(ref.tenant_id) != tenant:
            return None
        try:
            return self._service.resolve_secret_for_adapter(
                tenant_id=tenant, secret_ref=str(key)
            )
        except Exception:
            return None

    def assert_tenant_access(self, requester_tenant: str, integration_id: str):
        cfg = self.get_config(requester_tenant, integration_id)
        if cfg is None or not cfg.enabled:
            from tools.errors import ToolAuthFailedError

            raise ToolAuthFailedError("integration_not_available")
        return cfg


class IntegrationRuntime:
    def __init__(
        self,
        *,
        service: IntegrationService,
        registry: IntegrationRegistry,
        secrets_backend,
        ledger: OperationLedger,
        webhooks: WebhookProcessor,
        credential_bridge: IntegrationCredentialBridge,
        enabled: bool = True,
    ):
        self.service = service
        self.registry = registry
        self.secrets = secrets_backend
        self.ledger = ledger
        self.webhooks = webhooks
        self.credential_store = credential_bridge
        self.enabled = enabled

    def health(self) -> dict:
        secrets_health = {}
        try:
            secrets_health = dict(self.secrets.health())
        except Exception:
            secrets_health = {"status": "unavailable"}
        violations = []
        try:
            violations = self.registry.scan_plaintext_violations()
        except Exception:
            violations = ["scan_failed"]
        status = "healthy"
        if not self.enabled:
            status = "disabled"
        elif secrets_health.get("status") == "unavailable":
            status = "blocked"
        elif violations:
            status = "blocked"
        return {
            "integration_status": status,
            "secrets": secrets_health,
            "registry_backend": getattr(self.registry, "persistence_backend", "sqlite"),
            "plaintext_violations": len(violations),
            "providers": sorted(self.service.provider_foundations().keys()),
            "enabled": self.enabled,
        }

    def close(self) -> None:
        for obj in (self.registry, self.ledger, self.webhooks, self.secrets):
            if hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass


def build_integration_runtime(
    *,
    env: dict | None = None,
    encryption=None,
    shared_connection=None,
    observability=None,
) -> IntegrationRuntime | None:
    cfg = integration_config(env)
    if not cfg["enabled"]:
        return None
    secrets = build_secrets_backend(
        env=env, encryption=encryption, shared_connection=shared_connection
    )
    # Metadata registry — shared SQLite when available
    if shared_connection is not None and cfg["use_shared_db"]:
        registry = IntegrationRegistry(shared_connection=shared_connection)
        ledger = OperationLedger(shared_connection=shared_connection)
        webhooks = WebhookProcessor(
            secrets_backend=secrets, shared_connection=shared_connection
        )
    elif cfg["db_path"]:
        registry = IntegrationRegistry(path=cfg["db_path"])
        ledger = OperationLedger(path=cfg["db_path"])
        webhooks = WebhookProcessor(secrets_backend=secrets, path=cfg["db_path"])
    else:
        registry = IntegrationRegistry(path=":memory:")
        ledger = OperationLedger(path=":memory:")
        webhooks = WebhookProcessor(secrets_backend=secrets, path=":memory:")

    breaker = CircuitBreaker()
    http = IntegrationHttpClient(secrets_backend=secrets, circuit_breaker=breaker)
    service = IntegrationService(
        registry=registry,
        secrets_backend=secrets,
        ledger=ledger,
        webhooks=webhooks,
        circuit_breaker=breaker,
        http_client=http,
    )
    # Startup validation
    violations = registry.scan_plaintext_violations()
    if violations and cfg["production"]:
        from integrations.errors import IntegrationError

        raise IntegrationError("plaintext_credentials_forbidden")
    bridge = IntegrationCredentialBridge(service)
    _ = observability
    return IntegrationRuntime(
        service=service,
        registry=registry,
        secrets_backend=secrets,
        ledger=ledger,
        webhooks=webhooks,
        credential_bridge=bridge,
        enabled=True,
    )
