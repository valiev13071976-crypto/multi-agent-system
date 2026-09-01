"""LIVE Ozon adapter — dormant without configuration."""

from __future__ import annotations

from typing import Callable

from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.ozon.client import OzonHttpClient
from integrations.ozon.config import OzonIntegrationConfig, load_ozon_config
from integrations.ozon.fixture_adapter import OzonFixtureAdapter


class LiveOzonAdapter(OzonFixtureAdapter):
    def __init__(
        self,
        *,
        config: OzonIntegrationConfig | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
    ):
        super().__init__()
        self._config = config or load_ozon_config()
        self._secret_resolver = secret_resolver
        self._client: OzonHttpClient | None = None
        self.environment = "LIVE"
        self.live = True

    @property
    def client(self) -> OzonHttpClient:
        if self._client is None:
            self._client = OzonHttpClient(config=self._config, secret_resolver=self._secret_resolver)
        return self._client

    def _assert_live_configured(self, credential_ref: str = "") -> None:
        if not self._config.is_live:
            raise IntegrationNotConfiguredError("ozon_live_mode_required")
        if not self._config.live_configured:
            raise IntegrationNotConfiguredError("ozon_credentials_not_configured")

    def verify(self, *, credential_ref: str) -> dict:
        try:
            self._assert_live_configured(credential_ref)
        except IntegrationNotConfiguredError as exc:
            return {"ok": False, "category": exc.code}
        if not self.state.auth_ok:
            return {"ok": False, "category": "INTEGRATION_AUTH_FAILED"}
        return {
            "ok": True,
            "authentication_valid": True,
            "required_capabilities_available": True,
            "provider_identity": "live:ozon",
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
        data = self.client.post("/v2/posting/fbs/list", body={"limit": 10}, credential_ref=credential_ref)
        return {"items": data.get("result") or [], "mode": "LIVE", "live": True, "provider_metadata": self._config.safe_metadata()}

    def write(self, *, capability: str, payload: dict, idempotency_key: str, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured(credential_ref)
        raise IntegrationNotConfiguredError("ozon_live_write_blocked_engineering")
