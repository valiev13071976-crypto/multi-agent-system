"""Document retention defaults."""

from __future__ import annotations

from datetime import datetime, timedelta

from memory.models import utc_now


class DocumentRetentionPolicy:
    def __init__(self, *, default_ttl_days: int = 365):
        self.default_ttl_days = int(default_ttl_days)

    def expires_at(self, *, now: datetime | None = None) -> datetime:
        stamp = now or utc_now()
        return stamp + timedelta(days=self.default_ttl_days)


def document_policy_snapshot() -> dict:
    return {
        "document_policy_version": "1.0.0",
        "default_ttl_days": 365,
        "raw_binary_default": False,
        "ocr_default": False,
        "formula_execution": False,
        "macros_allowed": False,
        "external_link_fetch": False,
        "rules": [
            "scoped_access_only",
            "no_network_ingestion",
            "no_ocr_silent",
            "no_formula_execution",
            "no_macros",
            "bounded_extraction",
        ],
    }
