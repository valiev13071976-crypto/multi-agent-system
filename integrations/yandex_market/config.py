"""Yandex Market integration configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class YandexMarketIntegrationConfig:
    mode: str
    api_base_url: str
    oauth_token_ref: str
    campaign_id_ref: str
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
        return str(os.environ.get("YANDEX_MARKET_OAUTH_TOKEN") or "").strip()

    def safe_metadata(self) -> dict:
        return {
            "mode": self.mode,
            "live": self.is_live,
            "live_configured": self.live_configured,
            "verify_tls": self.verify_tls,
            "timeout_seconds": self.timeout_seconds,
            "endpoints_configured": bool(self.api_base_url),
        }


def load_yandex_market_config(env: dict | None = None) -> YandexMarketIntegrationConfig:
    e = env if env is not None else os.environ
    mode = str(e.get("YANDEX_MARKET_INTEGRATION_MODE") or "FIXTURE").strip().upper()
    return YandexMarketIntegrationConfig(
        mode=mode,
        api_base_url=str(e.get("YANDEX_MARKET_API_BASE_URL") or "https://api.partner.market.yandex.ru").strip(),
        oauth_token_ref="YANDEX_MARKET_OAUTH_TOKEN",
        campaign_id_ref="YANDEX_MARKET_CAMPAIGN_ID",
        timeout_seconds=float(e.get("YANDEX_MARKET_TIMEOUT_SECONDS") or 30),
        verify_tls=str(e.get("YANDEX_MARKET_VERIFY_TLS", "true")).lower() not in {"0", "false", "no"},
    )


def yandex_market_engineering_ready() -> bool:
    return True


def yandex_market_live_active() -> bool:
    cfg = load_yandex_market_config()
    return cfg.is_live and cfg.live_configured


def yandex_market_live_verified() -> bool:
    return False
