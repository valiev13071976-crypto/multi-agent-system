"""LIVE Yandex Market adapter — dormant without configuration."""

from __future__ import annotations

from typing import Callable

from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.yandex_market.client import YandexMarketHttpClient
from integrations.yandex_market.config import YandexMarketIntegrationConfig, load_yandex_market_config
from integrations.yandex_market.fixture_adapter import YandexMarketFixtureAdapter


class LiveYandexMarketAdapter(YandexMarketFixtureAdapter):
    def __init__(
        self,
        *,
        config: YandexMarketIntegrationConfig | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
    ):
        super().__init__()
        self._config = config or load_yandex_market_config()
        self._secret_resolver = secret_resolver
        self._client: YandexMarketHttpClient | None = None
        self.environment = "LIVE"
        self.live = True

    @property
    def client(self) -> YandexMarketHttpClient:
        if self._client is None:
            self._client = YandexMarketHttpClient(config=self._config, secret_resolver=self._secret_resolver)
        return self._client

    def _assert_live_configured(self, credential_ref: str = "") -> None:
        if not self._config.is_live:
            raise IntegrationNotConfiguredError("yandex_market_live_mode_required")
        if not self._config.live_configured:
            raise IntegrationNotConfiguredError("yandex_market_oauth_not_configured")

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
            "provider_identity": "live:yandex_market",
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
        data = self.client.get("/campaigns", credential_ref=credential_ref)
        return {"items": data.get("campaigns") or [], "mode": "LIVE", "live": True, "provider_metadata": self._config.safe_metadata()}

    def write(self, *, capability: str, payload: dict, idempotency_key: str, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured(credential_ref)
        raise IntegrationNotConfiguredError("yandex_market_live_write_blocked_engineering")
