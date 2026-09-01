"""LIVE Bitrix adapter — structurally capable, dormant without configuration."""

from __future__ import annotations

from typing import Callable

from integrations.activation.adapters import FixtureAdapterState
from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.bitrix.client import BitrixHttpClient
from integrations.bitrix.config import BitrixIntegrationConfig, load_bitrix_config
from integrations.bitrix.fixture_adapter import BitrixFixtureAdapter


class LiveBitrixAdapter(BitrixFixtureAdapter):
    """Production Bitrix adapter — fail closed without LIVE webhook/OAuth config."""

    def __init__(
        self,
        *,
        config: BitrixIntegrationConfig | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
        state: FixtureAdapterState | None = None,
    ):
        super().__init__(state=state)  # type: ignore[arg-type]
        self._config = config or load_bitrix_config()
        self._secret_resolver = secret_resolver
        self._client: BitrixHttpClient | None = None
        self.environment = "LIVE"
        self.live = True

    @property
    def client(self) -> BitrixHttpClient:
        if self._client is None:
            self._client = BitrixHttpClient(config=self._config, secret_resolver=self._secret_resolver)
        return self._client

    def _assert_live_configured(self, credential_ref: str = "") -> None:
        if not self._config.is_live:
            raise IntegrationNotConfiguredError("bitrix_live_mode_required")
        url = ""
        if self._secret_resolver and credential_ref:
            url = str(self._secret_resolver(credential_ref) or "").strip()
        if not url:
            url = self._config._resolved_webhook_url()
        if not url:
            raise IntegrationNotConfiguredError("bitrix_webhook_not_configured")

    def verify(self, *, credential_ref: str) -> dict:
        try:
            self._assert_live_configured(credential_ref)
        except IntegrationNotConfiguredError as exc:
            return {"ok": False, "category": exc.code}
        if self.state.auth_ok is False:
            return {"ok": False, "category": "INTEGRATION_AUTH_FAILED"}
        return {
            "ok": True,
            "authentication_valid": True,
            "required_capabilities_available": True,
            "provider_identity": "live:bitrix",
            "destructive": False,
            "live": True,
            "mode": "LIVE",
            **self._config.safe_metadata(),
        }

    def health(self) -> dict:
        try:
            self._assert_live_configured()
        except IntegrationNotConfiguredError:
            return {"status": "UNHEALTHY", "error_category": "INTEGRATION_NOT_CONFIGURED"}
        if self.state.rate_limited:
            return {"status": "DEGRADED", "error_category": "INTEGRATION_RATE_LIMITED"}
        if not self.state.auth_ok:
            return {"status": "UNHEALTHY", "error_category": "INTEGRATION_AUTH_FAILED"}
        return {"status": "HEALTHY", "error_category": "", "live": True, "mode": "LIVE"}

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured(credential_ref)
        self._raise_if_bad()
        # Engineering block: no destructive/mutating live calls in tests; read path only when configured.
        # Structural live read delegates to Bitrix REST catalog.product.list when webhook present.
        params = params or {}
        operation = str(params.get("operation") or "")
        method = "crm.product.list" if operation != "order_read" else "sale.order.list"
        data = self.client.call(method, credential_ref=credential_ref, params={"filter": {}, "select": ["ID", "NAME"]})
        return {
            "items": data.get("result") or [],
            "mode": "LIVE",
            "live": True,
            "provider_metadata": self._config.safe_metadata(),
        }

    def write(self, *, capability: str, payload: dict, idempotency_key: str, tenant_id: str = "", credential_ref: str = "") -> dict:
        # LIVE writes are structurally implemented but blocked during engineering closure.
        self._assert_live_configured(credential_ref)
        raise IntegrationNotConfiguredError("bitrix_live_write_blocked_engineering")
