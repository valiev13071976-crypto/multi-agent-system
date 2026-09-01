"""Calendar integration configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CalendarIntegrationConfig:
    mode: str
    api_base_url: str
    oauth_token_ref: str
    timeout_seconds: float
    verify_tls: bool

    @property
    def is_live(self) -> bool:
        return self.mode.upper() == "LIVE"

    @property
    def live_configured(self) -> bool:
        if not self.is_live:
            return False
        return bool(self._resolved_token() and self.api_base_url)

    def _resolved_token(self) -> str:
        return str(os.environ.get("CALENDAR_OAUTH_TOKEN") or "").strip()

    def safe_metadata(self) -> dict:
        return {"mode": self.mode, "live": self.is_live, "live_configured": self.live_configured}


def load_calendar_config(env: dict | None = None) -> CalendarIntegrationConfig:
    e = env if env is not None else os.environ
    mode = str(e.get("CALENDAR_INTEGRATION_MODE") or "FIXTURE").strip().upper()
    return CalendarIntegrationConfig(
        mode=mode,
        api_base_url=str(e.get("CALENDAR_API_BASE_URL") or "https://www.googleapis.com").strip(),
        oauth_token_ref="CALENDAR_OAUTH_TOKEN",
        timeout_seconds=float(e.get("CALENDAR_TIMEOUT_SECONDS") or 30),
        verify_tls=str(e.get("CALENDAR_VERIFY_TLS", "true")).lower() not in {"0", "false", "no"},
    )


def calendar_live_active() -> bool:
    cfg = load_calendar_config()
    return cfg.is_live and cfg.live_configured


def calendar_live_verified() -> bool:
    return False
