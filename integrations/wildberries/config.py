"""Wildberries integration configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WildberriesIntegrationConfig:
    mode: str
    api_base_url: str
    content_api_url: str
    marketplace_api_url: str
    token_ref: str
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
        return str(os.environ.get("WILDBERRIES_API_TOKEN") or "").strip()

    def safe_metadata(self) -> dict:
        return {
            "mode": self.mode,
            "live": self.is_live,
            "live_configured": self.live_configured,
            "verify_tls": self.verify_tls,
            "timeout_seconds": self.timeout_seconds,
            "endpoints_configured": bool(self.api_base_url),
        }


def load_wildberries_config(env: dict | None = None) -> WildberriesIntegrationConfig:
    e = env if env is not None else os.environ
    mode = str(e.get("WILDBERRIES_INTEGRATION_MODE") or "FIXTURE").strip().upper()
    return WildberriesIntegrationConfig(
        mode=mode,
        api_base_url=str(e.get("WILDBERRIES_API_BASE_URL") or "https://suppliers-api.wildberries.ru").strip(),
        content_api_url=str(e.get("WILDBERRIES_CONTENT_API_URL") or "").strip(),
        marketplace_api_url=str(e.get("WILDBERRIES_MARKETPLACE_API_URL") or "").strip(),
        token_ref="WILDBERRIES_API_TOKEN",
        timeout_seconds=float(e.get("WILDBERRIES_TIMEOUT_SECONDS") or 30),
        verify_tls=str(e.get("WILDBERRIES_VERIFY_TLS", "true")).lower() not in {"0", "false", "no"},
    )
