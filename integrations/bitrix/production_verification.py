"""Block 5.6 final defect closure — standalone LIVE Bitrix schema-binding
production verification entry point.

Root cause this module fixes: ``BitrixProductBridge.verify_schema_binding``
(and every other read/write on the bridge) routes exclusively through
``IntegrationActivationService.execute_via_gateway`` ->
``resolve_connection``, which requires an existing, ACTIVE_ELIGIBLE
``IntegrationConnection`` record for the tenant/provider/environment. A
freshly-constructed ``IntegrationActivationService()`` starts with zero
connection records, so even though ``LiveBitrixAdapter`` is already
correctly configured from protected environment variables (proven by
calling it directly), ``execute_via_gateway`` has nothing to resolve and
raises ``IntegrationNotConfiguredError`` before ever reaching the adapter.

This is NOT a new connector/architecture. It reuses the exact same
governed connection lifecycle every other provider already uses
(``configure_connection`` -> ``verify_connection`` -> ``activate_connection``
-- see ``tests/test_block5_6_bitrix_aspro_integration.py``'s ``_active()``
helper for the pre-existing FIXTURE-environment precedent). The one thing
that did not exist yet was a LIVE-environment, env-config-driven variant of
that same bootstrap for Bitrix specifically -- ``ensure_live_bitrix_connection``
below is exactly that, and nothing more:

- It never stores or returns a real secret. ``LIVE_CREDENTIAL_REF`` is a
  fixed, non-secret reference *name* (it must merely start with
  ``"secret:"`` to satisfy ``configure_connection``'s plaintext-credential
  guard); the actual webhook URL is resolved by ``LiveBitrixAdapter`` from
  protected environment configuration at call time, exactly as it already
  is on the direct-adapter path this task's PROVEN evidence exercised.
- It fails closed (``IntegrationNotConfiguredError``) if
  ``BitrixIntegrationConfig.live_configured`` is false -- it never
  registers a fake ACTIVE connection over missing/incomplete LIVE
  configuration.
- It is idempotent: an existing ACTIVE-eligible LIVE Bitrix connection for
  the tenant is reused rather than duplicated.
- It does not touch ``execute_via_gateway``, ``resolve_connection``, or any
  other provider's bootstrap -- normal gateway behavior for every existing
  caller (FIXTURE tests, other providers, explicit ``connection_id``
  callers) is completely unchanged.
"""

from __future__ import annotations

from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.activation.models import ACTIVE_ELIGIBLE, ENV_LIVE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.config import BitrixIntegrationConfig, load_bitrix_config
from integrations.bitrix.product_bridge import BitrixProductBridge

# Non-secret reference *name* only -- see module docstring. Never resolves
# to a real value through the activation service's secret store; the real
# webhook URL always comes from protected env via LiveBitrixAdapter itself.
LIVE_CREDENTIAL_REF = "secret:bitrix-live-webhook"

# This verification path has no per-customer tenant context (it proves the
# connector against the one production Bitrix account configured via
# protected env, not a specific Panda tenant's data) -- "production" is a
# fixed, non-secret label, not an invented/unknown tenant.
PRODUCTION_TENANT_ID = "production"


def ensure_live_bitrix_connection(
    activation: IntegrationActivationService,
    *,
    tenant_id: str = PRODUCTION_TENANT_ID,
    config: BitrixIntegrationConfig | None = None,
) -> str:
    """Idempotently register+verify+activate a LIVE Bitrix connection for
    ``tenant_id`` on ``activation`` from existing protected env
    configuration, returning its ``connection_id``. Reuses an existing
    ACTIVE-eligible LIVE connection if present. Raises
    ``IntegrationNotConfiguredError`` (fails closed) if LIVE mode/webhook
    configuration is not actually present -- never fabricates activation.
    """
    cfg = config or load_bitrix_config()
    if not cfg.live_configured:
        raise IntegrationNotConfiguredError("bitrix_live_not_configured")

    for conn in activation.list_connections(tenant_id=tenant_id, provider_id="bitrix"):
        if conn.environment == ENV_LIVE and conn.status in ACTIVE_ELIGIBLE:
            return conn.connection_id

    conn = activation.configure_connection(
        tenant_id=tenant_id,
        provider_id="bitrix",
        credential_ref=LIVE_CREDENTIAL_REF,
        environment=ENV_LIVE,
    )
    activation.verify_connection(tenant_id=tenant_id, connection_id=conn.connection_id)
    activation.activate_connection(tenant_id=tenant_id, connection_id=conn.connection_id)
    return conn.connection_id


def run_production_schema_verification(
    *,
    bitrix_product_id: str = "477",
    tenant_id: str = PRODUCTION_TENANT_ID,
    activation: IntegrationActivationService | None = None,
) -> dict:
    """One bounded, READ-only call: bootstrap (or reuse) the LIVE Bitrix
    connection from existing protected env config, then run
    ``BitrixProductBridge.verify_schema_binding`` for ``bitrix_product_id``.

    This is the single supported standalone production verification path --
    it performs no writes and never returns/logs a secret (the returned
    report only ever contains the same safe, already-redacted fields
    ``verify_schema_binding``/``BitrixIntegrationConfig.safe_metadata``
    already produce).
    """
    svc = activation or IntegrationActivationService()
    connection_id = ensure_live_bitrix_connection(svc, tenant_id=tenant_id)
    bridge = BitrixProductBridge(integration_activation=svc, environment=ENV_LIVE)
    report = bridge.verify_schema_binding(
        tenant_id=tenant_id, bitrix_product_id=bitrix_product_id, connection_id=connection_id
    )
    return {"tenant_id": tenant_id, "connection_id": connection_id, **report}
