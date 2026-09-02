"""Typed errors for scale optimization."""

from __future__ import annotations


class ScaleOptimizationError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


FORBIDDEN = "FORBIDDEN"
TENANT_SCOPE_VIOLATION = "TENANT_SCOPE_VIOLATION"
INVALID_METRIC = "INVALID_METRIC"
INVALID_COMPARISON = "INVALID_COMPARISON"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
LIVE_FALLBACK_FORBIDDEN = "LIVE_FALLBACK_FORBIDDEN"
INVALID_LABEL = "INVALID_LABEL"
INVALID_CONFIG = "INVALID_CONFIG"
