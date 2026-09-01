"""LIVE CRM adapter — dormant without configuration."""

from __future__ import annotations

from typing import Callable

from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.crm.client import CrmHttpClient
from integrations.crm.config import CrmIntegrationConfig, load_crm_config
from integrations.crm.fixture_adapter import CrmFixtureAdapter


class LiveCrmAdapter(CrmFixtureAdapter):
    def __init__(
        self,
        *,
        config: CrmIntegrationConfig | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
    ):
        super().__init__()
        self._config = config or load_crm_config()
        self._secret_resolver = secret_resolver
        self._client: CrmHttpClient | None = None
        self.environment = "LIVE"
        self.live = True

    def _assert_live_configured(self) -> None:
        if not self._config.is_live or not self._config.live_configured:
            raise IntegrationNotConfiguredError("crm_not_configured")

    def verify(self, *, credential_ref: str) -> dict:
        try:
            self._assert_live_configured()
        except IntegrationNotConfiguredError as exc:
            return {"ok": False, "category": exc.code}
        return {"ok": True, "live": True, "mode": "LIVE", **self._config.safe_metadata()}

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured()
        if self._client is None:
            self._client = CrmHttpClient(config=self._config, secret_resolver=self._secret_resolver)
        data = self._client.get("/crm/v3/objects/contacts", credential_ref=credential_ref)
        return {"items": data.get("results") or [], "mode": "LIVE", "live": True}

    def write(self, *, capability: str, payload: dict, idempotency_key: str, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured()
        raise IntegrationNotConfiguredError("crm_live_write_blocked_engineering")
