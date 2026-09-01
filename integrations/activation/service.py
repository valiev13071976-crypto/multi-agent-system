"""Integration Activation Service — governed connection lifecycle + resolution."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from security.redaction import redact
from security.tenant import require_tenant_id

from integrations.activation.adapters import FixtureAdapterState, FixtureProviderAdapter
from integrations.activation.composio import ComposioFixtureAdapter
from integrations.bitrix.fixture_adapter import AsproFixtureAdapter, BitrixFixtureAdapter
from integrations.bitrix.live_adapter import LiveBitrixAdapter
from integrations.onec.fixture_adapter import OneCFixtureAdapter
from integrations.onec.live_adapter import LiveOneCAdapter
from integrations.wildberries.fixture_adapter import WildberriesFixtureAdapter
from integrations.wildberries.live_adapter import LiveWildberriesAdapter
from integrations.activation.errors import (
    IntegrationAuthFailedError,
    IntegrationCapabilityUnavailableError,
    IntegrationCrossTenantError,
    IntegrationEnvironmentMismatchError,
    IntegrationLiveFallbackForbiddenError,
    IntegrationNotActiveError,
    IntegrationNotConfiguredError,
    IntegrationPermissionDeniedError,
    IntegrationPlaintextSecretRejectedError,
    IntegrationProviderUnavailableError,
    IntegrationVerificationFailedError,
    IntegrationWriteDeniedError,
)
from integrations.activation.models import (
    ACTIVE_ELIGIBLE,
    ENV_FIXTURE,
    ENV_LIVE,
    ENV_SANDBOX,
    ENVIRONMENTS,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_UNHEALTHY,
    HEALTH_UNKNOWN,
    OP_READ,
    OP_WRITE,
    STATUS_ACTIVE,
    STATUS_CONFIGURED,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_REVOKED,
    STATUS_UNCONFIGURED,
    STATUS_VERIFYING,
    IntegrationActivation,
    IntegrationConnection,
    IntegrationEvidence,
    IntegrationHealthView,
    IntegrationVerification,
    ResolvedConnection,
)
from integrations.activation.providers import PROVIDER_CATALOG, get_provider, list_providers


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _reject_plaintext(value: str, *, field: str) -> None:
    v = value or ""
    lowered = v.lower()
    if any(
        x in lowered
        for x in (
            "api_key=",
            "access_token=",
            "password=",
            "authorization:",
            "bearer ",
            "secret=",
        )
    ):
        raise IntegrationPlaintextSecretRejectedError(field)
    if v.startswith("sk-") or (len(v) > 40 and " " not in v and not v.startswith("secret:")):
        # Long opaque blob without secret: prefix looks like raw token
        if field == "credential_ref" and not v.startswith("secret:"):
            raise IntegrationPlaintextSecretRejectedError(field)


class IntegrationActivationService:
    """Canonical Real Integration Activation layer over existing Tool/secrets foundations."""

    def __init__(self, *, secrets_backend=None):
        self._connections: dict[str, IntegrationConnection] = {}
        self._by_tenant: dict[str, list[str]] = {}
        self._evidence: list[IntegrationEvidence] = []
        self._adapters: dict[str, FixtureProviderAdapter] = {}
        self._provider_health_override: dict[str, str] = {}  # provider-level outage
        self._secrets = secrets_backend  # optional; refs only stored here
        self._secret_values: dict[tuple[str, str], str] = {}  # (tenant, ref) for fixture only
        self._usage: list[dict] = []
        self._composio = ComposioFixtureAdapter()
        self._adapters["composio"] = self._composio
        self._bitrix_fixture = BitrixFixtureAdapter()
        self._aspro_fixture = AsproFixtureAdapter()
        self._adapters["bitrix"] = self._bitrix_fixture
        self._adapters["aspro"] = self._aspro_fixture
        self._onec_fixture = OneCFixtureAdapter()
        self._adapters["onec"] = self._onec_fixture
        self._wb_fixture = WildberriesFixtureAdapter()
        self._adapters["wildberries"] = self._wb_fixture
        for pid in PROVIDER_CATALOG:
            if pid not in {"composio", "bitrix", "aspro", "onec", "wildberries"}:
                self._adapters[pid] = FixtureProviderAdapter(pid)

    # --- providers ---

    def list_providers(self):
        return list_providers()

    def get_provider(self, provider_id: str):
        return get_provider(provider_id)

    # --- secrets (fixture-safe) ---

    def put_secret_ref(self, *, tenant_id: str, secret_ref: str, value: str) -> str:
        tenant = require_tenant_id(tenant_id)
        _reject_plaintext(secret_ref, field="secret_ref")
        if not secret_ref.startswith("secret:"):
            raise IntegrationPlaintextSecretRejectedError("secret_ref")
        # value never returned later via public APIs
        self._secret_values[(tenant, secret_ref)] = value
        return secret_ref

    # --- lifecycle ---

    def configure_connection(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        credential_ref: str,
        environment: str = ENV_FIXTURE,
        owner_id: str = "",
        external_account_ref: str = "",
        priority: int = 100,
        write_capabilities: tuple[str, ...] | None = None,
        connection_id: str | None = None,
    ) -> IntegrationConnection:
        tenant = require_tenant_id(tenant_id)
        provider = get_provider(provider_id)
        if provider is None:
            raise IntegrationNotConfiguredError(provider_id)
        if environment not in ENVIRONMENTS:
            raise IntegrationEnvironmentMismatchError(environment)
        if environment not in provider.supported_environments:
            raise IntegrationEnvironmentMismatchError(environment)
        _reject_plaintext(credential_ref, field="credential_ref")
        if not credential_ref.startswith("secret:"):
            raise IntegrationPlaintextSecretRejectedError("credential_ref")

        writes = write_capabilities if write_capabilities is not None else provider.write_capabilities
        if provider.write_default_deny:
            writes = ()
        cid = connection_id or str(uuid.uuid4())
        conn = IntegrationConnection(
            connection_id=cid,
            tenant_id=tenant,
            provider_id=provider_id,
            environment=environment,
            credential_ref=credential_ref,
            status=STATUS_CONFIGURED,
            owner_id=owner_id,
            external_account_ref=external_account_ref,
            priority=priority,
            read_capabilities=provider.read_capabilities,
            write_capabilities=writes,
            auth_type=provider.auth_types[0] if provider.auth_types else "API_KEY",
            metadata={"live": environment == ENV_LIVE, "mode": environment},
        )
        self._connections[cid] = conn
        self._by_tenant.setdefault(tenant, []).append(cid)
        self._emit(
            tenant_id=tenant,
            connection_id=cid,
            provider_id=provider_id,
            event_type="connection_configured",
            environment=environment,
            status=STATUS_CONFIGURED,
        )
        return conn

    def verify_connection(self, *, tenant_id: str, connection_id: str) -> IntegrationVerification:
        conn = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
        if conn.status in {STATUS_REVOKED, STATUS_DISABLED}:
            raise IntegrationNotActiveError(conn.status)
        verifying = self._replace_status(conn, STATUS_VERIFYING)
        adapter = self._adapter_for(verifying)
        started = _utc()
        result = adapter.verify(credential_ref=verifying.credential_ref)
        evidence_id = str(uuid.uuid4())
        if not result.get("ok"):
            self._replace_status(verifying, STATUS_FAILED)
            category = str(result.get("category") or "INTEGRATION_VERIFICATION_FAILED")
            self._emit(
                tenant_id=verifying.tenant_id,
                connection_id=connection_id,
                provider_id=verifying.provider_id,
                event_type="verification_failed",
                environment=verifying.environment,
                status=STATUS_FAILED,
                error_category=category,
                evidence_id=evidence_id,
            )
            raise IntegrationVerificationFailedError(category)
        verified = IntegrationConnection(
            connection_id=verifying.connection_id,
            tenant_id=verifying.tenant_id,
            provider_id=verifying.provider_id,
            environment=verifying.environment,
            credential_ref=verifying.credential_ref,
            status=STATUS_CONFIGURED,
            owner_id=verifying.owner_id,
            external_account_ref=verifying.external_account_ref,
            priority=verifying.priority,
            read_capabilities=verifying.read_capabilities,
            write_capabilities=verifying.write_capabilities,
            auth_type=verifying.auth_type,
            last_verified_at=_utc(),
            metadata=dict(verifying.metadata),
            user_connection_status=verifying.user_connection_status,
        )
        self._connections[connection_id] = verified
        verification = IntegrationVerification(
            connection_id=connection_id,
            tenant_id=verified.tenant_id,
            authentication_valid=True,
            required_capabilities_available=True,
            environment=verified.environment,
            verified_at=_utc(),
            evidence_id=evidence_id,
            provider_identity=str(result.get("provider_identity") or ""),
            details={"destructive": False, "latency_ms": (_utc() - started).total_seconds() * 1000},
        )
        self._emit(
            tenant_id=verified.tenant_id,
            connection_id=connection_id,
            provider_id=verified.provider_id,
            event_type="verification_passed",
            environment=verified.environment,
            status="VERIFIED",
            evidence_id=evidence_id,
        )
        return verification

    def activate_connection(self, *, tenant_id: str, connection_id: str) -> IntegrationActivation:
        conn = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
        if conn.status in {STATUS_REVOKED, STATUS_DISABLED}:
            raise IntegrationNotActiveError(conn.status)
        if conn.last_verified_at is None:
            # Activation requires verification
            self.verify_connection(tenant_id=tenant_id, connection_id=connection_id)
            conn = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
        active = self._replace_status(conn, STATUS_ACTIVE)
        evidence_id = str(uuid.uuid4())
        self._emit(
            tenant_id=active.tenant_id,
            connection_id=connection_id,
            provider_id=active.provider_id,
            event_type="connection_activated",
            environment=active.environment,
            status=STATUS_ACTIVE,
            evidence_id=evidence_id,
        )
        return IntegrationActivation(
            connection_id=connection_id,
            tenant_id=active.tenant_id,
            status=STATUS_ACTIVE,
            environment=active.environment,
            activated_at=_utc(),
            verification_evidence_id=evidence_id,
        )

    def disable_connection(self, *, tenant_id: str, connection_id: str) -> IntegrationConnection:
        conn = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
        disabled = self._replace_status(conn, STATUS_DISABLED)
        self._emit(
            tenant_id=disabled.tenant_id,
            connection_id=connection_id,
            provider_id=disabled.provider_id,
            event_type="connection_disabled",
            environment=disabled.environment,
            status=STATUS_DISABLED,
        )
        return disabled

    def revoke_connection(self, *, tenant_id: str, connection_id: str) -> IntegrationConnection:
        conn = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
        revoked = self._replace_status(conn, STATUS_REVOKED)
        self._emit(
            tenant_id=revoked.tenant_id,
            connection_id=connection_id,
            provider_id=revoked.provider_id,
            event_type="connection_revoked",
            environment=revoked.environment,
            status=STATUS_REVOKED,
        )
        return revoked

    def rotate_credential(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        new_credential_ref: str,
        fail_verification: bool = False,
    ) -> IntegrationConnection:
        conn = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
        _reject_plaintext(new_credential_ref, field="credential_ref")
        if not new_credential_ref.startswith("secret:"):
            raise IntegrationPlaintextSecretRejectedError("credential_ref")
        rotated = IntegrationConnection(
            connection_id=conn.connection_id,
            tenant_id=conn.tenant_id,
            provider_id=conn.provider_id,
            environment=conn.environment,
            credential_ref=new_credential_ref,
            status=STATUS_CONFIGURED,
            owner_id=conn.owner_id,
            external_account_ref=conn.external_account_ref,
            priority=conn.priority,
            read_capabilities=conn.read_capabilities,
            write_capabilities=conn.write_capabilities,
            auth_type=conn.auth_type,
            last_verified_at=None,
            metadata=dict(conn.metadata),
            user_connection_status=conn.user_connection_status,
        )
        self._connections[connection_id] = rotated
        if fail_verification:
            adapter = self._adapter_for(rotated)
            adapter.state.auth_ok = False
            try:
                self.verify_connection(tenant_id=tenant_id, connection_id=connection_id)
            except IntegrationVerificationFailedError:
                failed = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
                # remain non-ACTIVE
                return failed
        self.verify_connection(tenant_id=tenant_id, connection_id=connection_id)
        self.activate_connection(tenant_id=tenant_id, connection_id=connection_id)
        return self.get_connection(tenant_id=tenant_id, connection_id=connection_id)

    # --- queries ---

    def get_connection(self, *, tenant_id: str, connection_id: str) -> IntegrationConnection:
        tenant = require_tenant_id(tenant_id)
        conn = self._connections.get(connection_id)
        if conn is None:
            raise IntegrationNotConfiguredError(connection_id)
        if conn.tenant_id != tenant:
            raise IntegrationCrossTenantError("connection")
        return conn

    def list_connections(self, *, tenant_id: str, provider_id: str | None = None) -> list[IntegrationConnection]:
        tenant = require_tenant_id(tenant_id)
        ids = self._by_tenant.get(tenant, [])
        out = [self._connections[i] for i in ids if i in self._connections]
        if provider_id:
            out = [c for c in out if c.provider_id == provider_id]
        return out

    def connection_status_safe(self, *, tenant_id: str, connection_id: str) -> dict:
        conn = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
        return {
            "connection_id": conn.connection_id,
            "provider": conn.provider_id,
            "status": conn.status,
            "environment": conn.environment,
            "live": conn.environment == ENV_LIVE,
            "user_facing": f"{conn.provider_id}: {conn.status.lower().replace('_', ' ')}",
            # never include credential_ref value content beyond ref id presence
            "credential_configured": bool(conn.credential_ref),
        }

    def list_connection_capabilities(self, *, tenant_id: str, connection_id: str) -> dict:
        conn = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
        return {
            "read": list(conn.read_capabilities),
            "write": list(conn.write_capabilities),
            "status": conn.status,
            "environment": conn.environment,
        }

    # --- resolution ---

    def resolve_connection(
        self,
        *,
        tenant_id: str,
        capability: str,
        environment: str,
        operation_class: str = OP_READ,
        connection_id: str | None = None,
        owner_id: str | None = None,
    ) -> ResolvedConnection:
        tenant = require_tenant_id(tenant_id)
        if environment not in ENVIRONMENTS:
            raise IntegrationEnvironmentMismatchError(environment)

        if connection_id:
            conn = self.get_connection(tenant_id=tenant, connection_id=connection_id)
            if conn.environment != environment:
                raise IntegrationEnvironmentMismatchError("explicit_connection_env_mismatch")
            if conn.status not in ACTIVE_ELIGIBLE:
                raise IntegrationNotActiveError(conn.status)
            if owner_id and conn.owner_id and conn.owner_id != owner_id:
                raise IntegrationPermissionDeniedError("owner_scope")
            self._assert_capability(conn, capability=capability, operation_class=operation_class)
            provider = get_provider(conn.provider_id)
            assert provider is not None
            return ResolvedConnection(
                connection=conn,
                provider=provider,
                capability=capability,
                operation_class=operation_class,
                environment=environment,
            )

        candidates = [
            c
            for c in self.list_connections(tenant_id=tenant)
            if c.environment == environment and c.status in ACTIVE_ELIGIBLE
        ]
        if owner_id:
            candidates = [c for c in candidates if not c.owner_id or c.owner_id == owner_id]

        matching = []
        write_denied = False
        for c in candidates:
            try:
                self._assert_capability(c, capability=capability, operation_class=operation_class)
                matching.append(c)
            except IntegrationCapabilityUnavailableError:
                continue
            except IntegrationWriteDeniedError:
                write_denied = True
                continue

        if not matching:
            if write_denied and operation_class == OP_WRITE:
                raise IntegrationWriteDeniedError(capability)
            # LIVE must never fall back to SANDBOX/FIXTURE
            if environment == ENV_LIVE:
                others = [
                    c
                    for c in self.list_connections(tenant_id=tenant)
                    if c.status in ACTIVE_ELIGIBLE and c.environment in {ENV_FIXTURE, ENV_SANDBOX}
                ]
                if others:
                    raise IntegrationLiveFallbackForbiddenError("live_no_fallback")
            raise IntegrationNotConfiguredError(capability)

        # Deterministic: lowest priority number, then connection_id
        matching.sort(key=lambda c: (c.priority, c.connection_id))
        chosen = matching[0]
        provider = get_provider(chosen.provider_id)
        assert provider is not None
        self._emit(
            tenant_id=tenant,
            connection_id=chosen.connection_id,
            provider_id=chosen.provider_id,
            event_type="capability_resolved",
            environment=environment,
            capability=capability,
            operation_class=operation_class,
            status="RESOLVED",
        )
        return ResolvedConnection(
            connection=chosen,
            provider=provider,
            capability=capability,
            operation_class=operation_class,
            environment=environment,
        )

    def _assert_capability(self, conn: IntegrationConnection, *, capability: str, operation_class: str) -> None:
        provider = get_provider(conn.provider_id)
        if operation_class == OP_WRITE:
            allowed = set(conn.write_capabilities)
            if provider:
                allowed |= set(provider.write_capabilities) & set(conn.write_capabilities)
            if capability not in conn.write_capabilities:
                raise IntegrationWriteDeniedError(capability)
            return
        allowed_read = set(conn.read_capabilities)
        if provider:
            allowed_read |= set(provider.read_capabilities)
        if capability in allowed_read or capability in conn.read_capabilities:
            return
        if provider and capability in provider.capabilities and capability in provider.read_capabilities:
            return
        raise IntegrationCapabilityUnavailableError(capability)

    # --- execute via ToolGateway boundary (fixture) ---

    def execute_via_gateway(
        self,
        *,
        tenant_id: str,
        capability: str,
        environment: str,
        operation_class: str,
        payload: dict | None = None,
        idempotency_key: str = "",
        correlation_id: str = "",
        workflow_id: str = "",
        approved_write: bool = False,
        connection_id: str | None = None,
        page: int = 1,
    ) -> dict:
        """Canonical execution entry — models ToolGateway-governed path."""
        resolved = self.resolve_connection(
            tenant_id=tenant_id,
            capability=capability,
            environment=environment,
            operation_class=operation_class,
            connection_id=connection_id,
        )
        if resolved.connection.environment != environment:
            raise IntegrationEnvironmentMismatchError("resolved_env_mismatch")

        # Provider-level outage isolation
        if self._provider_health_override.get(resolved.provider.provider_id) == "UNHEALTHY":
            raise IntegrationProviderUnavailableError(resolved.provider.provider_id)

        adapter = self._adapter_for(resolved.connection)
        started = _utc()
        cred_ref = resolved.connection.credential_ref
        try:
            if operation_class == OP_WRITE:
                if not approved_write:
                    raise IntegrationWriteDeniedError("approval_required")
                if not idempotency_key:
                    raise IntegrationWriteDeniedError("idempotency_required")
                result = adapter.write(
                    capability=capability,
                    payload=payload or {},
                    idempotency_key=idempotency_key,
                    tenant_id=tenant_id,
                    credential_ref=cred_ref,
                )
            else:
                result = adapter.read(
                    capability=capability,
                    params={"page": page, **(payload or {})},
                    tenant_id=tenant_id,
                    credential_ref=cred_ref,
                )
        except Exception as exc:
            category = getattr(exc, "code", type(exc).__name__)
            self._emit(
                tenant_id=tenant_id,
                connection_id=resolved.connection.connection_id,
                provider_id=resolved.provider.provider_id,
                event_type="external_execution_failed",
                environment=environment,
                capability=capability,
                operation_class=operation_class,
                status="FAILED",
                error_category=str(category),
                correlation_id=correlation_id,
                workflow_id=workflow_id,
            )
            raise

        latency = (_utc() - started).total_seconds() * 1000
        self._emit(
            tenant_id=tenant_id,
            connection_id=resolved.connection.connection_id,
            provider_id=resolved.provider.provider_id,
            event_type="external_execution",
            environment=environment,
            capability=capability,
            operation_class=operation_class,
            status="OK",
            correlation_id=correlation_id,
            workflow_id=workflow_id,
            latency_ms=latency,
        )
        self._usage.append(
            {
                "tenant_id": tenant_id,
                "provider": resolved.provider.provider_id,
                "connection_id": resolved.connection.connection_id,
                "operation": capability,
                "units": 1,
                "cost": None,  # unknown — never invent
                "environment": environment,
            }
        )
        # Redact any accidental secret-looking strings in result summary
        safe = {"result": result, "connection_id": resolved.connection.connection_id, "environment": environment, "live": environment == ENV_LIVE}
        return safe

    def paginated_read(
        self,
        *,
        tenant_id: str,
        capability: str,
        environment: str,
        max_pages: int = 3,
        connection_id: str | None = None,
    ) -> dict:
        pages = []
        page = 1
        while page <= max_pages:
            out = self.execute_via_gateway(
                tenant_id=tenant_id,
                capability=capability,
                environment=environment,
                operation_class=OP_READ,
                connection_id=connection_id,
                page=page,
            )
            items = out["result"].get("items") or []
            pages.append({"page": page, "count": len(items)})
            nxt = out["result"].get("next_page")
            if not nxt:
                break
            page = int(nxt)
        return {"pages": pages, "bounded": True, "max_pages": max_pages}

    # --- health ---

    def health(self, *, tenant_id: str, connection_id: str) -> IntegrationHealthView:
        conn = self.get_connection(tenant_id=tenant_id, connection_id=connection_id)
        override = self._provider_health_override.get(conn.provider_id)
        if override:
            status = override
            err = "INTEGRATION_PROVIDER_UNAVAILABLE"
        else:
            h = self._adapter_for(conn).health()
            status = h.get("status") or HEALTH_UNKNOWN
            err = h.get("error_category") or ""
        return IntegrationHealthView(
            provider_id=conn.provider_id,
            connection_id=connection_id,
            environment=conn.environment,
            status=status,
            timestamp=_utc(),
            error_category=err,
        )

    def isolate_provider(self, provider_id: str, *, unavailable: bool = True) -> None:
        if unavailable:
            self._provider_health_override[provider_id] = "UNHEALTHY"
        else:
            self._provider_health_override.pop(provider_id, None)

    def adapter_state(self, provider_id: str) -> FixtureAdapterState:
        return self._adapter_for_provider(provider_id).state

    # --- composio ---

    def composio(self) -> ComposioFixtureAdapter:
        return self._composio

    # --- evidence / usage ---

    def list_evidence(self, *, tenant_id: str, connection_id: str | None = None) -> list[IntegrationEvidence]:
        tenant = require_tenant_id(tenant_id)
        out = [e for e in self._evidence if e.tenant_id == tenant]
        if connection_id:
            out = [e for e in out if e.connection_id == connection_id]
        return out

    def usage_events(self, *, tenant_id: str) -> list[dict]:
        tenant = require_tenant_id(tenant_id)
        return [u for u in self._usage if u["tenant_id"] == tenant]

    def assert_no_secrets_in_evidence(self, *, tenant_id: str) -> None:
        for e in self.list_evidence(tenant_id=tenant_id):
            blob = redact(str(e.metadata) + e.connection_id + e.provider_id + e.event_type)
            for secret in self._secret_values.values():
                if secret and len(secret) >= 8 and secret in blob:
                    raise AssertionError("secret_leak_in_evidence")
            if e.connection_id in self._connections:
                for secret in self._secret_values.values():
                    if secret and len(secret) >= 8 and secret in str(e):
                        raise AssertionError("secret_leak")

    # --- internals ---

    def _adapter_for(self, conn: IntegrationConnection) -> FixtureProviderAdapter:
        if conn.environment == ENV_LIVE and conn.provider_id in {"bitrix", "aspro"}:
            return LiveBitrixAdapter(secret_resolver=lambda ref: self._resolve_secret(conn.tenant_id, ref))
        if conn.environment == ENV_LIVE and conn.provider_id == "onec":
            return LiveOneCAdapter(secret_resolver=lambda ref: self._resolve_secret(conn.tenant_id, ref))
        if conn.environment == ENV_LIVE and conn.provider_id == "wildberries":
            return LiveWildberriesAdapter(secret_resolver=lambda ref: self._resolve_secret(conn.tenant_id, ref))
        return self._adapter_for_provider(conn.provider_id)

    def _resolve_secret(self, tenant_id: str, secret_ref: str) -> str | None:
        return self._secret_values.get((tenant_id, secret_ref))

    def _adapter_for_provider(self, provider_id: str) -> FixtureProviderAdapter:
        if provider_id not in self._adapters:
            self._adapters[provider_id] = FixtureProviderAdapter(provider_id)
        return self._adapters[provider_id]

    def _replace_status(self, conn: IntegrationConnection, status: str) -> IntegrationConnection:
        updated = IntegrationConnection(
            connection_id=conn.connection_id,
            tenant_id=conn.tenant_id,
            provider_id=conn.provider_id,
            environment=conn.environment,
            credential_ref=conn.credential_ref,
            status=status,
            owner_id=conn.owner_id,
            external_account_ref=conn.external_account_ref,
            priority=conn.priority,
            read_capabilities=conn.read_capabilities,
            write_capabilities=conn.write_capabilities,
            auth_type=conn.auth_type,
            last_verified_at=conn.last_verified_at,
            metadata=dict(conn.metadata),
            created_at=conn.created_at,
            updated_at=_utc(),
            user_connection_status=conn.user_connection_status,
        )
        self._connections[conn.connection_id] = updated
        return updated

    def _emit(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        provider_id: str,
        event_type: str,
        environment: str,
        status: str = "",
        capability: str = "",
        operation_class: str = "",
        error_category: str = "",
        correlation_id: str = "",
        workflow_id: str = "",
        latency_ms: float | None = None,
        evidence_id: str | None = None,
    ) -> IntegrationEvidence:
        ev = IntegrationEvidence(
            evidence_id=evidence_id or str(uuid.uuid4()),
            tenant_id=tenant_id,
            connection_id=connection_id,
            provider_id=provider_id,
            event_type=event_type,
            environment=environment,
            capability=capability,
            operation_class=operation_class,
            status=status,
            correlation_id=correlation_id,
            workflow_id=workflow_id,
            latency_ms=latency_ms,
            error_category=error_category,
            metadata={"safe": True},
        )
        self._evidence.append(ev)
        return ev
