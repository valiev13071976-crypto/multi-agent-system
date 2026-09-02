"""Analytics dashboard errors."""

from __future__ import annotations


class AnalyticsError(Exception):
    code = "ANALYTICS_ERROR"
    http_status = 400

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


INVALID_ANALYTICS_QUERY = "INVALID_ANALYTICS_QUERY"
UNSUPPORTED_METRIC = "UNSUPPORTED_METRIC"
UNSUPPORTED_DIMENSION = "UNSUPPORTED_DIMENSION"
INVALID_TIME_RANGE = "INVALID_TIME_RANGE"
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
STALE_DATA = "STALE_DATA"
FORBIDDEN = "FORBIDDEN"
TENANT_SCOPE_VIOLATION = "TENANT_SCOPE_VIOLATION"
LIVE_FALLBACK_FORBIDDEN = "LIVE_FALLBACK_FORBIDDEN"
