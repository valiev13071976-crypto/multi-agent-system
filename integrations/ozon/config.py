"""Ozon integration configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OzonIntegrationConfig:
    mode: str
    api_base_url: str
    client_id_ref: str
    api_key_ref: str
    timeout_seconds: float
    verify_tls: bool

    @property
    def is_live(self) -> bool:
        return self.mode.upper() == "LIVE"

    @property
    def live_configured(self) -> bool:
        if not self.is_live:
            return False
        return bool(self._resolved_client_id() and self._resolved_api_key() and self.api_base_url)

    def _resolved_client_id(self) -> str:
        return str(os.environ.get("OZON_CLIENT_ID") or "").strip()

    def _resolved_api_key(self) -> str:
        return str(os.environ.get("OZON_API_KEY") or "").strip()

    def safe_metadata(self) -> dict:
        return {
            "mode": self.mode,
            "live": self.is_live,
            "live_configured": self.live_configured,
            "verify_tls": self.verify_tls,
            "timeout_seconds": self.timeout_seconds,
            "endpoints_configured": bool(self.api_base_url),
        }


def load_ozon_config(env: dict | None = None) -> OzonIntegrationConfig:
    e = env if env is not None else os.environ
    mode = str(e.get("OZON_INTEGRATION_MODE") or "FIXTURE").strip().upper()
    return OzonIntegrationConfig(
        mode=mode,
        api_base_url=str(e.get("OZON_API_BASE_URL") or "https://api-seller.ozon.ru").strip(),
        client_id_ref="OZON_CLIENT_ID",
        api_key_ref="OZON_API_KEY",
        timeout_seconds=float(e.get("OZON_TIMEOUT_SECONDS") or 30),
        verify_tls=str(e.get("OZON_VERIFY_TLS", "true")).lower() not in {"0", "false", "no"},
    )


def ozon_engineering_ready() -> bool:
    """Fixture E2E proven — engineering layer complete."""
    return True


def ozon_live_active() -> bool:
    """True only when LIVE mode is explicitly configured with credentials."""
    cfg = load_ozon_config()
    return cfg.is_live and cfg.live_configured


def ozon_live_verified() -> bool:
    """Never true during engineering closure without live verification."""
    return False
