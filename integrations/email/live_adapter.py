"""LIVE Email adapter — dormant without configuration."""

from __future__ import annotations

from typing import Callable

from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.email.client import EmailHttpClient
from integrations.email.config import EmailIntegrationConfig, load_email_config
from integrations.email.fixture_adapter import EmailFixtureAdapter


class LiveEmailAdapter(EmailFixtureAdapter):
    def __init__(
        self,
        *,
        config: EmailIntegrationConfig | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
    ):
        super().__init__()
        self._config = config or load_email_config()
        self._secret_resolver = secret_resolver
        self._client: EmailHttpClient | None = None
        self.environment = "LIVE"
        self.live = True

    @property
    def client(self) -> EmailHttpClient:
        if self._client is None:
            self._client = EmailHttpClient(config=self._config, secret_resolver=self._secret_resolver)
        return self._client

    def _assert_live_configured(self, credential_ref: str = "") -> None:
        if not self._config.is_live:
            raise IntegrationNotConfiguredError("email_live_mode_required")
        if not self._config.live_configured:
            raise IntegrationNotConfiguredError("email_oauth_not_configured")

    def verify(self, *, credential_ref: str) -> dict:
        try:
            self._assert_live_configured(credential_ref)
        except IntegrationNotConfiguredError as exc:
            return {"ok": False, "category": exc.code}
        if not self.state.auth_ok:
            return {"ok": False, "category": "INTEGRATION_AUTH_FAILED"}
        return {"ok": True, "live": True, "mode": "LIVE", **self._config.safe_metadata()}

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured(credential_ref)
        self._raise_if_bad()
        data = self.client.get("/gmail/v1/users/me/messages", credential_ref=credential_ref)
        return {"items": data.get("messages") or [], "mode": "LIVE", "live": True}

    def write(self, *, capability: str, payload: dict, idempotency_key: str, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured(credential_ref)
        raise IntegrationNotConfiguredError("email_live_write_blocked_engineering")
