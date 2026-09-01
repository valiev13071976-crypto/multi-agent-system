"""Bitrix integration configuration — names only, no hardcoded secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BitrixIntegrationConfig:
    mode: str  # FIXTURE | SANDBOX | LIVE
    base_url: str
    auth_mode: str  # webhook | oauth
    webhook_url_ref: str  # env key name or secret ref — never raw URL in code
    client_id_ref: str
    client_secret_ref: str
    timeout_seconds: float
    verify_tls: bool
    catalog_id: str
    site_id: str
    aspro_premier_enabled: bool
    aspro_field_mappings_ref: str

    @property
    def is_live(self) -> bool:
        return self.mode.upper() == "LIVE"

    @property
    def live_configured(self) -> bool:
        if not self.is_live:
            return False
        if self.auth_mode == "webhook":
            return bool(self._resolved_webhook_url())
        return bool(self.client_id_ref and self.client_secret_ref)

    def _resolved_webhook_url(self) -> str:
        key = self.webhook_url_ref or "BITRIX_WEBHOOK_URL"
        return str(os.environ.get(key) or "").strip()

    def safe_metadata(self) -> dict:
        return {
            "mode": self.mode,
            "auth_mode": self.auth_mode,
            "base_url_configured": bool(self.base_url),
            "catalog_id": self.catalog_id or "",
            "site_id": self.site_id or "",
            "aspro_premier_enabled": self.aspro_premier_enabled,
            "live": self.is_live,
            "live_configured": self.live_configured,
            "verify_tls": self.verify_tls,
            "timeout_seconds": self.timeout_seconds,
        }


def load_bitrix_config(env: dict | None = None) -> BitrixIntegrationConfig:
    e = env if env is not None else os.environ
    mode = str(e.get("BITRIX_INTEGRATION_MODE") or "FIXTURE").strip().upper()
    return BitrixIntegrationConfig(
        mode=mode,
        base_url=str(e.get("BITRIX_BASE_URL") or "").strip(),
        auth_mode=str(e.get("BITRIX_AUTH_MODE") or "webhook").strip().lower(),
        webhook_url_ref="BITRIX_WEBHOOK_URL",
        client_id_ref=str(e.get("BITRIX_CLIENT_ID") or "").strip(),
        client_secret_ref=str(e.get("BITRIX_CLIENT_SECRET") or "").strip(),
        timeout_seconds=float(e.get("BITRIX_TIMEOUT_SECONDS") or 30),
        verify_tls=str(e.get("BITRIX_VERIFY_TLS", "true")).lower() not in {"0", "false", "no"},
        catalog_id=str(e.get("BITRIX_CATALOG_ID") or "").strip(),
        site_id=str(e.get("BITRIX_SITE_ID") or "").strip(),
        aspro_premier_enabled=str(e.get("ASPRO_PREMIER_ENABLED", "")).lower() in {"1", "true", "yes"},
        aspro_field_mappings_ref=str(e.get("ASPRO_PREMIER_FIELD_MAPPINGS") or "").strip(),
    )
