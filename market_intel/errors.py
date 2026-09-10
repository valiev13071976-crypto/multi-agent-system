"""Market Intelligence error codes — stable, fail-closed."""

from __future__ import annotations

MI_DISABLED = "mi_disabled"
MI_LIVE_FORBIDDEN = "mi_live_forbidden"
MI_CLIENT_UNAVAILABLE = "mi_client_unavailable"
MI_SESSION_MISSING = "mi_session_missing"
MI_SESSION_ENCRYPTION_REQUIRED = "mi_session_encryption_required"
MI_ACCESS_DENIED = "mi_access_denied"
MI_CHANNEL_NOT_FOUND = "mi_channel_not_found"
MI_CHANNEL_NOT_MONITORED = "mi_channel_not_monitored"
MI_PRODUCT_NOT_FOUND = "mi_product_not_found"
MI_NO_COMPARABLE_OBSERVATIONS = "mi_no_comparable_observations"
MI_CATALOG_UNAVAILABLE = "mi_catalog_unavailable"


class MarketIntelError(Exception):
    def __init__(self, code: str, message: str = "", *, http_status: int = 400):
        self.code = code
        self.message = message or code
        self.http_status = http_status
        super().__init__(self.message)
