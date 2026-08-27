"""Integration service — registry + secrets + auth + HTTP + webhooks + ledger."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Mapping

from integrations.auth import OAuth2AuthStrategy, get_auth_strategy
from integrations.circuit_breaker import CircuitBreaker
from integrations.contracts import (
    CREDENTIAL_ACTIVE,
    CREDENTIAL_REVOKED,
    HEALTH_AUTH_FAILED,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_UNAVAILABLE,
    IntegrationCredentialRef,
    IntegrationDescriptor,
    IntegrationHealth,
    IntegrationOperationContext,
)
from integrations.errors import (
    AuthenticationFailedError,
    CredentialInvalidError,
    CredentialMissingError,
    IntegrationAccessDeniedError,
    IntegrationDisabledError,
    ScopeInsufficientError,
    SecretBackendUnavailableError,
)
from integrations.http_client import IntegrationHttpClient
from integrations.ledger import OperationLedger, fingerprint_request
from integrations.providers import PROVIDER_CONTRACTS, FakeProviderAdapter, get_provider_contract
from integrations.registry import IntegrationRegistry
from integrations.webhooks import WebhookProcessor
from security.tenant import normalize_tenant_id


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class IntegrationService:
    def __init__(
        self,
        *,
        registry: IntegrationRegistry,
        secrets_backend,
        ledger: OperationLedger | None = None,
        webhooks: WebhookProcessor | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        http_client: IntegrationHttpClient | None = None,
        deny_write_providers: frozenset[str] | None = None,
    ):
        self.registry = registry
        self.secrets = secrets_backend
        self.ledger = ledger or OperationLedger()
        self.webhooks = webhooks or WebhookProcessor(secrets_backend=secrets_backend)
        self.breaker = circuit_breaker or CircuitBreaker()
        self.http = http_client or IntegrationHttpClient(
            secrets_backend=secrets_backend, circuit_breaker=self.breaker
        )
        self._deny_write = deny_write_providers or frozenset({"bank", "payment_gateway", "payment"})
        self._oauth = OAuth2AuthStrategy()
        self._fakes: dict[str, FakeProviderAdapter] = {
            pid: FakeProviderAdapter(c) for pid, c in PROVIDER_CONTRACTS.items()
        }

    def register_integration(
        self, descriptor: IntegrationDescriptor, *, actor: str = ""
    ) -> IntegrationDescriptor:
        return self.registry.register(descriptor, actor=actor)

    def put_secret(
        self,
        *,
        tenant_id: str,
        secret_ref: str,
        value: str,
        credential_type: str = "api_key",
    ) -> IntegrationCredentialRef:
        handle = self.secrets.put_secret(
            tenant_id=tenant_id,
            secret_ref=secret_ref,
            value=value,
            credential_type=credential_type,
        )
        return IntegrationCredentialRef(
            credential_id=f"cred-{handle.secret_ref}",
            tenant_id=handle.tenant_id,
            secret_backend=handle.backend,
            secret_ref=handle.secret_ref,
            credential_type=credential_type,
            version=handle.version,
            rotation_state=CREDENTIAL_ACTIVE,
        )

    def resolve_secret_for_adapter(
        self, *, tenant_id: str, secret_ref: str, version: int | None = None
    ) -> str:
        """Internal-only resolution — callers must not log/serialize the value."""
        tenant = normalize_tenant_id(tenant_id)
        try:
            value = self.secrets.get_secret(
                tenant_id=tenant, secret_ref=secret_ref, version=version
            )
        except SecretBackendUnavailableError:
            raise
        except CredentialInvalidError:
            raise
        if value is None:
            raise CredentialMissingError("credential_missing")
        return value

    def assert_capability(
        self,
        descriptor: IntegrationDescriptor,
        *,
        capability: str,
        is_write: bool,
    ) -> None:
        if is_write:
            if descriptor.provider in self._deny_write or descriptor.adapter_id in self._deny_write:
                raise ScopeInsufficientError("scope_insufficient")
            if capability not in descriptor.write_capabilities:
                raise ScopeInsufficientError("scope_insufficient")
        else:
            if capability and capability not in descriptor.read_capabilities:
                # allow empty capability for generic read health
                if capability:
                    raise ScopeInsufficientError("scope_insufficient")

    def get_integration(self, tenant_id: str, integration_id: str) -> IntegrationDescriptor:
        desc = self.registry.get(tenant_id, integration_id)
        if desc is None:
            raise IntegrationAccessDeniedError("integration_access_denied")
        return desc

    async def invoke(
        self,
        *,
        tenant_id: str,
        integration_id: str,
        operation: str,
        method: str = "GET",
        path: str = "/",
        body: Mapping | None = None,
        capability: str = "",
        is_write: bool = False,
        idempotency_key: str = "",
        workflow_id: str = "",
        request_id: str = "",
        source_ip: str | None = None,
        oauth_refresh_token: str = "",
        oauth_expires_at: datetime | None = None,
    ) -> dict:
        desc = self.get_integration(tenant_id, integration_id)
        if not desc.enabled:
            raise IntegrationDisabledError("integration_disabled")
        self.assert_capability(desc, capability=capability, is_write=is_write)
        if is_write and not idempotency_key:
            from integrations.errors import IdempotencyConflictError

            raise IdempotencyConflictError("idempotency_conflict")

        ctx = IntegrationOperationContext(
            tenant_id=tenant_id,
            integration_id=integration_id,
            operation=operation,
            request_id=request_id or str(uuid.uuid4()),
            workflow_id=workflow_id,
            idempotency_key=idempotency_key,
            capabilities=(capability,) if capability else (),
            is_write=is_write,
        )
        fp = fingerprint_request(operation, dict(body or {}))
        op = self.ledger.begin(
            tenant_id=tenant_id,
            integration_id=integration_id,
            operation_type=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=fp,
            workflow_id=workflow_id,
            request_id=ctx.request_id,
        )
        if op.get("status") in {"completed", "succeeded"}:
            return {
                "operation_id": op["operation_id"],
                "status": op["status"],
                "idempotent_replay": True,
                "result": __import__("json").loads(op.get("result_json") or "{}"),
            }

        secret = None
        if desc.credential_ref:
            secret = self.resolve_secret_for_adapter(
                tenant_id=tenant_id, secret_ref=desc.credential_ref
            )
            if desc.auth_strategy == "oauth2" and oauth_refresh_token:
                secret = self._oauth.ensure_access_token(
                    cache_key=f"{tenant_id}:{integration_id}",
                    access_token=secret,
                    refresh_token=oauth_refresh_token,
                    expires_at=oauth_expires_at,
                    settings=dict(desc.safe_settings),
                )

        retries = 0
        try:
            result = await self.http.request(
                desc,
                ctx,
                method=method,
                path=path,
                json_body=body,
                source_ip=source_ip,
                secret=secret,
            )
            self.ledger.complete(
                op["operation_id"],
                tenant_id=tenant_id,
                status="completed",
                result={"status_code": result.get("status_code"), "truncated": result.get("truncated")},
                retries=retries,
            )
            return {"operation_id": op["operation_id"], "status": "completed", "result": result}
        except AuthenticationFailedError:
            # One safe refresh retry for OAuth
            if desc.auth_strategy == "oauth2" and oauth_refresh_token and self._oauth._refresh_fn:
                retries = 1
                secret = self._oauth.ensure_access_token(
                    cache_key=f"{tenant_id}:{integration_id}",
                    access_token="",
                    refresh_token=oauth_refresh_token,
                    expires_at=_utc(),
                    settings=dict(desc.safe_settings),
                )
                result = await self.http.request(
                    desc,
                    ctx,
                    method=method,
                    path=path,
                    json_body=body,
                    source_ip=source_ip,
                    secret=secret,
                    retry_attempt=1,
                )
                self.ledger.complete(
                    op["operation_id"],
                    tenant_id=tenant_id,
                    status="completed",
                    result={"status_code": result.get("status_code")},
                    retries=retries,
                )
                return {"operation_id": op["operation_id"], "status": "completed", "result": result}
            self.ledger.complete(
                op["operation_id"],
                tenant_id=tenant_id,
                status="failed",
                error_code="authentication_failed",
            )
            raise
        except Exception as exc:
            code = getattr(exc, "code", "external_permanent_failure")
            self.ledger.complete(
                op["operation_id"], tenant_id=tenant_id, status="failed", error_code=code
            )
            raise

    def check_health(self, tenant_id: str, integration_id: str) -> IntegrationHealth:
        start = time.monotonic()
        try:
            desc = self.get_integration(tenant_id, integration_id)
        except IntegrationAccessDeniedError:
            health = IntegrationHealth(
                status=HEALTH_UNAVAILABLE, last_check=_utc(), error_code="integration_not_found"
            )
            return health
        if not desc.enabled:
            health = IntegrationHealth(
                status=HEALTH_DEGRADED, last_check=_utc(), error_code="integration_disabled"
            )
            self.registry.set_health(tenant_id, integration_id, health)
            return health
        # config exists
        if desc.credential_ref:
            try:
                secret = self.secrets.get_secret(
                    tenant_id=tenant_id, secret_ref=desc.credential_ref
                )
            except SecretBackendUnavailableError:
                health = IntegrationHealth(
                    status=HEALTH_UNAVAILABLE,
                    last_check=_utc(),
                    error_code="secret_backend_unavailable",
                )
                self.registry.set_health(tenant_id, integration_id, health)
                return health
            except CredentialInvalidError:
                health = IntegrationHealth(
                    status=HEALTH_AUTH_FAILED,
                    last_check=_utc(),
                    error_code="credential_invalid",
                )
                self.registry.set_health(tenant_id, integration_id, health)
                return health
            if secret is None:
                health = IntegrationHealth(
                    status=HEALTH_AUTH_FAILED,
                    last_check=_utc(),
                    error_code="credential_missing",
                )
                self.registry.set_health(tenant_id, integration_id, health)
                return health
        fake = self._fakes.get(desc.provider)
        latency = (time.monotonic() - start) * 1000
        if fake is not None:
            fh = fake.health()
            status = fh.get("status", HEALTH_HEALTHY)
            health = IntegrationHealth(
                status=status if status in {
                    HEALTH_HEALTHY, HEALTH_DEGRADED, HEALTH_UNAVAILABLE, HEALTH_AUTH_FAILED
                } else HEALTH_HEALTHY,
                last_check=_utc(),
                latency_ms=latency,
                error_code="" if status == HEALTH_HEALTHY else status,
            )
        else:
            health = IntegrationHealth(
                status=HEALTH_HEALTHY, last_check=_utc(), latency_ms=latency
            )
        self.registry.set_health(tenant_id, integration_id, health)
        return health

    def rotate_secret(
        self,
        *,
        tenant_id: str,
        secret_ref: str,
        new_value: str,
        validate: bool = True,
        rollback_on_failure: bool = True,
    ) -> IntegrationCredentialRef:
        meta = self.secrets.metadata(tenant_id=tenant_id, secret_ref=secret_ref)
        previous_version = meta.version if meta else None
        try:
            handle = self.secrets.rotate_secret(
                tenant_id=tenant_id, secret_ref=secret_ref, new_value=new_value
            )
            if validate and not new_value:
                raise CredentialInvalidError("credential_invalid")
            return IntegrationCredentialRef(
                credential_id=f"cred-{handle.secret_ref}",
                tenant_id=handle.tenant_id,
                secret_backend=handle.backend,
                secret_ref=handle.secret_ref,
                version=handle.version,
                rotation_state=CREDENTIAL_ACTIVE,
            )
        except Exception:
            if rollback_on_failure and previous_version is not None:
                self.secrets.set_rotation_state(
                    tenant_id=tenant_id,
                    secret_ref=secret_ref,
                    state=CREDENTIAL_ACTIVE,
                    version=previous_version,
                )
                # Reactivate previous as active version via put of marker — backends keep previous
                if hasattr(self.secrets, "_active_version"):
                    self.secrets._active_version[
                        (normalize_tenant_id(tenant_id), secret_ref)
                    ] = previous_version
            raise

    def revoke_secret(self, *, tenant_id: str, secret_ref: str) -> None:
        self.secrets.set_rotation_state(
            tenant_id=tenant_id, secret_ref=secret_ref, state=CREDENTIAL_REVOKED
        )

    def provider_foundations(self) -> dict:
        return {
            pid: {
                "provider_id": c.provider_id,
                "adapter_id": c.adapter_id,
                "supported_auth": list(c.supported_auth),
                "read": list(c.default_read_capabilities),
                "write": list(c.default_write_capabilities),
                "write_default_deny": c.write_default_deny,
            }
            for pid, c in PROVIDER_CONTRACTS.items()
        }
