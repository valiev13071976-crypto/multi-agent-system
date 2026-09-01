"""LIVE Calendar adapter — dormant without configuration."""

from __future__ import annotations

from typing import Callable

from integrations.activation.errors import IntegrationNotConfiguredError
from integrations.calendar.client import CalendarHttpClient
from integrations.calendar.config import CalendarIntegrationConfig, load_calendar_config
from integrations.calendar.fixture_adapter import CalendarFixtureAdapter


class LiveCalendarAdapter(CalendarFixtureAdapter):
    def __init__(
        self,
        *,
        config: CalendarIntegrationConfig | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
    ):
        super().__init__()
        self._config = config or load_calendar_config()
        self._secret_resolver = secret_resolver
        self._client: CalendarHttpClient | None = None
        self.environment = "LIVE"
        self.live = True

    def _assert_live_configured(self) -> None:
        if not self._config.is_live or not self._config.live_configured:
            raise IntegrationNotConfiguredError("calendar_not_configured")

    def verify(self, *, credential_ref: str) -> dict:
        try:
            self._assert_live_configured()
        except IntegrationNotConfiguredError as exc:
            return {"ok": False, "category": exc.code}
        return {"ok": True, "live": True, "mode": "LIVE", **self._config.safe_metadata()}

    def read(self, *, capability: str, params: dict | None = None, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured()
        if self._client is None:
            self._client = CalendarHttpClient(config=self._config, secret_resolver=self._secret_resolver)
        data = self._client.get("/calendar/v3/users/me/calendarList", credential_ref=credential_ref)
        return {"items": data.get("items") or [], "mode": "LIVE", "live": True}

    def write(self, *, capability: str, payload: dict, idempotency_key: str, tenant_id: str = "", credential_ref: str = "") -> dict:
        self._assert_live_configured()
        raise IntegrationNotConfiguredError("calendar_live_write_blocked_engineering")
