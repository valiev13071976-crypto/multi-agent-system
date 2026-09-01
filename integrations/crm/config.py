"""CRM integration configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CrmIntegrationConfig:
    mode: str
    api_base_url: str
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
        return bool(self._resolved_key() and self.api_base_url)

    def _resolved_key(self) -> str:
        return str(os.environ.get("CRM_API_KEY") or "").strip()

    def safe_metadata(self) -> dict:
        return {"mode": self.mode, "live": self.is_live, "live_configured": self.live_configured}


def load_crm_config(env: dict | None = None) -> CrmIntegrationConfig:
    e = env if env is not None else os.environ
    mode = str(e.get("CRM_INTEGRATION_MODE") or "FIXTURE").strip().upper()
    return CrmIntegrationConfig(
        mode=mode,
        api_base_url=str(e.get("CRM_API_BASE_URL") or "https://api.hubspot.com").strip(),
        api_key_ref="CRM_API_KEY",
        timeout_seconds=float(e.get("CRM_TIMEOUT_SECONDS") or 30),
        verify_tls=str(e.get("CRM_VERIFY_TLS", "true")).lower() not in {"0", "false", "no"},
    )


def crm_live_active() -> bool:
    cfg = load_crm_config()
    return cfg.is_live and cfg.live_configured


def crm_live_verified() -> bool:
    return False
