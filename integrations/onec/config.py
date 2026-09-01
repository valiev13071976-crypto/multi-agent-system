"""1C integration configuration — names only, no hardcoded secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OneCIntegrationConfig:
    mode: str
    base_url: str
    transport: str  # http_rest | odata | commerceml
    auth_mode: str  # basic | bearer | oauth
    username_ref: str
    password_ref: str
    token_ref: str
    client_id_ref: str
    client_secret_ref: str
    timeout_seconds: float
    verify_tls: bool
    catalog_id: str
    organization_id: str
    warehouse_mappings_ref: str
    price_type_mappings_ref: str

    @property
    def is_live(self) -> bool:
        return self.mode.upper() == "LIVE"

    @property
    def live_configured(self) -> bool:
        if not self.is_live:
            return False
        url = self._resolved_base_url()
        if not url:
            return False
        if self.auth_mode == "bearer":
            return bool(self._resolved_token())
        if self.auth_mode == "basic":
            return bool(self._resolved_username() and self._resolved_password())
        return bool(self.client_id_ref and self.client_secret_ref)

    def _resolved_base_url(self) -> str:
        return str(os.environ.get("ONEC_BASE_URL") or self.base_url or "").strip()

    def _resolved_token(self) -> str:
        return str(os.environ.get("ONEC_TOKEN") or os.environ.get("ONEC_API_TOKEN") or "").strip()

    def _resolved_username(self) -> str:
        return str(os.environ.get("ONEC_USERNAME") or "").strip()

    def _resolved_password(self) -> str:
        return str(os.environ.get("ONEC_PASSWORD") or "").strip()

    def safe_metadata(self) -> dict:
        return {
            "mode": self.mode,
            "transport": self.transport,
            "auth_mode": self.auth_mode,
            "base_url_configured": bool(self._resolved_base_url()),
            "organization_id": self.organization_id or "",
            "catalog_id": self.catalog_id or "",
            "live": self.is_live,
            "live_configured": self.live_configured,
            "verify_tls": self.verify_tls,
            "timeout_seconds": self.timeout_seconds,
        }


def load_onec_config(env: dict | None = None) -> OneCIntegrationConfig:
    e = env if env is not None else os.environ
    mode = str(e.get("ONEC_INTEGRATION_MODE") or "FIXTURE").strip().upper()
    return OneCIntegrationConfig(
        mode=mode,
        base_url=str(e.get("ONEC_BASE_URL") or e.get("ONEC_API_URL") or "").strip(),
        transport=str(e.get("ONEC_TRANSPORT") or "http_rest").strip().lower(),
        auth_mode=str(e.get("ONEC_AUTH_MODE") or "basic").strip().lower(),
        username_ref="ONEC_USERNAME",
        password_ref="ONEC_PASSWORD",
        token_ref="ONEC_TOKEN",
        client_id_ref=str(e.get("ONEC_CLIENT_ID") or "").strip(),
        client_secret_ref=str(e.get("ONEC_CLIENT_SECRET") or "").strip(),
        timeout_seconds=float(e.get("ONEC_TIMEOUT_SECONDS") or 30),
        verify_tls=str(e.get("ONEC_VERIFY_TLS", "true")).lower() not in {"0", "false", "no"},
        catalog_id=str(e.get("ONEC_CATALOG_ID") or "").strip(),
        organization_id=str(e.get("ONEC_ORGANIZATION_ID") or "").strip(),
        warehouse_mappings_ref=str(e.get("ONEC_WAREHOUSE_MAPPINGS") or "").strip(),
        price_type_mappings_ref=str(e.get("ONEC_PRICE_TYPE_MAPPINGS") or "").strip(),
    )
