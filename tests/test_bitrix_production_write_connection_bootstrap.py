"""Production defect closure — first real Bitrix write failed with
``«Запись в Bitrix не удалась: cms.bitrix.catalog.write. Товар не создан.»``
(HTTP 200, no traceback) despite LIVE Bitrix credentials being correctly
configured via protected env.

Root cause reproduced/fixed here: every governed Bitrix read/write routes
through ``IntegrationActivationService.execute_via_gateway`` ->
``resolve_connection``, which requires an existing, ACTIVE
``IntegrationConnection`` record for the *calling* tenant. The real
conversational write path (``business_assistant.controlled_bitrix_write.
execute_single_product_write`` -> ``BitrixProductBridge.sync_product``)
never bootstrapped one for the real per-request tenant on the actual
``IntegrationActivationService`` instance the running app uses -- only a
separate, standalone, manually-invoked verification entry point
(``integrations.bitrix.production_verification.
run_production_schema_verification``, fixed ``tenant_id="production"``,
its own throwaway activation service) ever called
``ensure_live_bitrix_connection``. ``resolve_connection`` therefore found
zero candidates for the real tenant and raised
``IntegrationNotConfiguredError(capability)`` -- whose ``.code`` becomes
the raw capability string itself (``IntegrationError.__init__``: a
positional ``code`` argument overrides the class-level default) --
*before* the live adapter was ever reached, matching "HTTP 200, no
traceback" exactly (the exception is caught and normalized by
``execute_single_product_write``, never propagated as an unhandled 500).

Fix: ``BitrixProductBridge.ensure_live_connection_ready`` (explicit,
caller-invoked, idempotent, no-op for FIXTURE/SANDBOX) reuses the exact
same, already-tested ``ensure_live_bitrix_connection`` bootstrap, now
called for the *real* tenant right before the real write.
``execute_single_product_write`` calls it; ``main.py`` selects
``ENV_LIVE`` for the Bitrix bridge specifically when Bitrix is genuinely
live-configured (previously hardcoded to the shared, always-FIXTURE
``BusinessAssistantService.integration_environment``).

No real network calls anywhere in this file. ``LiveBitrixAdapter.write()``
for ``product_create`` is now really implemented (a later PR closed the
``bitrix_live_write_blocked_engineering`` placeholder that operation used
to hit -- see ``tests/test_bitrix_live_product_create_write.py`` for that
implementation's full coverage); the two tests below that exercise
``execute_single_product_write`` all the way through therefore mock
``BoundedHttpClient.request`` with a deterministic, controlled failure so
they still make ZERO real network calls while proving the connection-
resolution fix (getting PAST ``resolve_connection`` into the real adapter)
independently of whatever ``product_create`` itself does next.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import httpx

from business_assistant.controlled_bitrix_write import (
    STATUS_WRITE_FAILED,
    SingleProductWriteRequest,
    execute_single_product_write,
    format_bitrix_write_result_text,
)
from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.activation.models import ENV_FIXTURE, ENV_LIVE, OP_WRITE
from integrations.activation.service import IntegrationActivationService
from integrations.bitrix.product_bridge import BitrixProductBridge
from integrations.bitrix.production_verification import LIVE_CREDENTIAL_REF
from integrations.production.http import BoundedHttpClient


def _always_service_unavailable(method: str, url: str, **kwargs) -> httpx.Response:
    """Deterministic, controlled HTTP failure -- proves the write path
    reaches the real adapter/transport without ever making (or needing) a
    real network call, and without depending on product_create's own
    downstream behavior."""
    return httpx.Response(503)

REAL_TENANT_ID = "tenant-real-customer"


class _LiveEnv:
    """Same env shape production Railway configuration uses for a
    live-configured Bitrix integration; restores prior values on exit."""

    KEYS = ("BITRIX_INTEGRATION_MODE", "BITRIX_WEBHOOK_URL", "BITRIX_CATALOG_ID", "BITRIX_OFFERS_IBLOCK_ID")

    def __enter__(self):
        self._prior = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["BITRIX_INTEGRATION_MODE"] = "LIVE"
        os.environ["BITRIX_WEBHOOK_URL"] = "https://panda.msk.ru/rest/1/totally-fake-test-secret-never-real/"
        os.environ["BITRIX_CATALOG_ID"] = "14"
        os.environ["BITRIX_OFFERS_IBLOCK_ID"] = "15"
        return self

    def __exit__(self, *exc):
        for k, v in self._prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _NoLiveEnv:
    def __enter__(self):
        self._prior = {k: os.environ.pop(k, None) for k in _LiveEnv.KEYS}
        return self

    def __exit__(self, *exc):
        for k, v in self._prior.items():
            if v is not None:
                os.environ[k] = v


def _write_request(**overrides) -> SingleProductWriteRequest:
    base = dict(
        tenant_id=REAL_TENANT_ID,
        title="Телевизор LG 32LQ63006LA.ARUG",
        sku="32LQ63006LA.ARUG",
        retail_price="29990",
    )
    base.update(overrides)
    return SingleProductWriteRequest(**base)


class EnsureLiveConnectionReadyTests(unittest.TestCase):
    """Direct coverage of the new opt-in bootstrap method."""

    def test_noop_for_fixture_environment_registers_nothing(self):
        activation = IntegrationActivationService()
        bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE)
        bridge.ensure_live_connection_ready(tenant_id=REAL_TENANT_ID)
        self.assertEqual(activation.list_connections(tenant_id=REAL_TENANT_ID, provider_id="bitrix"), [])

    def test_registers_an_active_live_connection_for_the_real_tenant(self):
        with _LiveEnv():
            activation = IntegrationActivationService()
            bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_LIVE)
            self.assertEqual(activation.list_connections(tenant_id=REAL_TENANT_ID, provider_id="bitrix"), [])
            bridge.ensure_live_connection_ready(tenant_id=REAL_TENANT_ID)
            conns = activation.list_connections(tenant_id=REAL_TENANT_ID, provider_id="bitrix")
            self.assertEqual(len(conns), 1)
            self.assertEqual(conns[0].environment, ENV_LIVE)
            self.assertEqual(conns[0].status, "ACTIVE")
            self.assertEqual(conns[0].credential_ref, LIVE_CREDENTIAL_REF)

    def test_is_idempotent_across_repeated_calls(self):
        with _LiveEnv():
            activation = IntegrationActivationService()
            bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_LIVE)
            bridge.ensure_live_connection_ready(tenant_id=REAL_TENANT_ID)
            bridge.ensure_live_connection_ready(tenant_id=REAL_TENANT_ID)
            self.assertEqual(len(activation.list_connections(tenant_id=REAL_TENANT_ID, provider_id="bitrix")), 1)

    def test_fails_closed_when_live_config_is_genuinely_absent(self):
        with _NoLiveEnv():
            activation = IntegrationActivationService()
            bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_LIVE)
            with self.assertRaises(IntegrationNotConfiguredError):
                bridge.ensure_live_connection_ready(tenant_id=REAL_TENANT_ID)
            self.assertEqual(activation.list_connections(tenant_id=REAL_TENANT_ID, provider_id="bitrix"), [])


class ProductionDefectReproductionAndFixTests(unittest.TestCase):
    """Reproduces the exact reported production symptom against a fresh,
    never-bootstrapped activation service for the real tenant, then proves
    the fix moves the failure past connection resolution."""

    def test_exact_reported_symptom_is_reproducible_pre_bootstrap(self):
        """Direct proof of root cause: resolving the write capability for a
        tenant with zero registered connections raises
        IntegrationNotConfiguredError whose ``.code`` is the bare
        capability string -- exactly the opaque text Panda showed the
        OWNER in production."""
        with _LiveEnv():
            activation = IntegrationActivationService()
            bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_LIVE)
            with self.assertRaises(IntegrationNotConfiguredError) as ctx:
                activation.execute_via_gateway(
                    tenant_id=REAL_TENANT_ID,
                    capability="cms.bitrix.catalog.write",
                    environment=ENV_LIVE,
                    operation_class=OP_WRITE,
                    payload={},
                    idempotency_key="k",
                    approved_write=True,
                )
            self.assertEqual(ctx.exception.code, "cms.bitrix.catalog.write")

    def test_execute_single_product_write_no_longer_fails_at_connection_resolution(self):
        """The fixed flow: with no pre-existing connection, the controlled
        write path now bootstraps one automatically and gets PAST
        resolve_connection, into the real adapter/transport (mocked here
        with a deterministic 503 so this makes zero real network calls,
        independent of whatever product_create itself does downstream --
        see tests/test_bitrix_live_product_create_write.py for that)."""
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=_always_service_unavailable):
            activation = IntegrationActivationService()
            bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_LIVE)
            result = execute_single_product_write(
                bridge, tenant_id=REAL_TENANT_ID, request=_write_request(), approved=True
            )
            self.assertEqual(result["status"], STATUS_WRITE_FAILED)
            # Must NOT be the old opaque capability-string failure anymore.
            self.assertNotEqual(result["error"], "cms.bitrix.catalog.write")
            # A connection now exists for the real tenant (bootstrap ran).
            conns = activation.list_connections(tenant_id=REAL_TENANT_ID, provider_id="bitrix")
            self.assertEqual(len(conns), 1)
            self.assertEqual(conns[0].environment, ENV_LIVE)

    def test_user_facing_diagnostic_no_longer_shows_bare_capability_string(self):
        with _LiveEnv(), patch.object(BoundedHttpClient, "request", side_effect=_always_service_unavailable):
            activation = IntegrationActivationService()
            bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_LIVE)
            result = execute_single_product_write(
                bridge, tenant_id=REAL_TENANT_ID, request=_write_request(), approved=True
            )
            text = format_bitrix_write_result_text(result)
            # The old, unhelpful collapse-to-just-the-capability message.
            self.assertNotIn("Запись в Bitrix не удалась: cms.bitrix.catalog.write.", text)

    def test_bootstrap_is_scoped_to_the_real_tenant_only(self):
        with _LiveEnv():
            activation = IntegrationActivationService()
            bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_LIVE)
            execute_single_product_write(bridge, tenant_id=REAL_TENANT_ID, request=_write_request(), approved=True)
            self.assertEqual(activation.list_connections(tenant_id="some-other-tenant", provider_id="bitrix"), [])

    def test_fixture_environment_write_flow_is_completely_unaffected(self):
        """Directly-affected regression guard: the pre-existing, fully
        governed FIXTURE write/read-back flow (PR #43) must remain
        untouched by this fix."""
        from integrations.bitrix.catalog import BitrixCatalogStore
        from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter

        store = BitrixCatalogStore()
        activation = IntegrationActivationService()
        activation._adapters["bitrix"] = BitrixFixtureAdapter(store=store)
        ref = activation.put_secret_ref(tenant_id="tenant-a", secret_ref="secret:bitrix-tenant-a", value="tok")
        conn = activation.configure_connection(
            tenant_id="tenant-a", provider_id="bitrix", credential_ref=ref, environment=ENV_FIXTURE
        )
        activation.verify_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        activation.activate_connection(tenant_id="tenant-a", connection_id=conn.connection_id)
        bridge = BitrixProductBridge(integration_activation=activation, environment=ENV_FIXTURE, store=store)

        result = execute_single_product_write(
            bridge, tenant_id="tenant-a", request=_write_request(tenant_id="tenant-a"), approved=True
        )
        self.assertEqual(result["status"], "WRITE_VERIFIED")
        self.assertTrue(result["mutated"])


class MainWiringEnvironmentSelectionTests(unittest.TestCase):
    """Proves main.py selects ENV_LIVE for the Bitrix bridge specifically
    when Bitrix is genuinely live-configured, and keeps the previous
    ENV_FIXTURE default otherwise -- without touching
    ``BusinessAssistantService.integration_environment`` used by every
    other connector (email/calendar/CRM/marketplaces/1C)."""

    def _reload_main(self):
        import importlib

        import main as main_mod

        return importlib.reload(main_mod)

    def test_defaults_to_fixture_without_live_bitrix_config(self):
        import tempfile

        with _NoLiveEnv():
            tmp = tempfile.mkdtemp()
            os.environ["BA_API_DB_PATH"] = os.path.join(tmp, "ba_api.sqlite")
            os.environ["BA_API_UPLOAD_DIR"] = os.path.join(tmp, "ba_uploads")
            mod = self._reload_main()
            bridge = mod.ba_api_runtime.service.ba.bitrix_product_bridge
            self.assertIsNotNone(bridge)
            self.assertEqual(bridge.environment, ENV_FIXTURE)

    def test_selects_live_when_bitrix_is_live_configured(self):
        import tempfile

        with _LiveEnv():
            tmp = tempfile.mkdtemp()
            os.environ["BA_API_DB_PATH"] = os.path.join(tmp, "ba_api.sqlite")
            os.environ["BA_API_UPLOAD_DIR"] = os.path.join(tmp, "ba_uploads")
            mod = self._reload_main()
            bridge = mod.ba_api_runtime.service.ba.bitrix_product_bridge
            self.assertIsNotNone(bridge)
            self.assertEqual(bridge.environment, ENV_LIVE)
            # The shared integration_environment used by every other
            # connector must be completely unaffected by this Bitrix-only
            # selection.
            self.assertEqual(mod.ba_api_runtime.service.ba.integration_environment, ENV_FIXTURE)


if __name__ == "__main__":
    unittest.main()
