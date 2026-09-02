"""Scheduled business automation configuration."""

from __future__ import annotations

import os

MIN_INTERVAL_SECONDS = 60
MAX_CATCH_UP_OCCURRENCES = 3
WORKFLOW_TYPE_BUSINESS_AUTOMATION = "business_automation.run"


def _mode() -> str:
    return str(os.environ.get("SCHEDULED_AUTOMATION_MODE") or "FIXTURE").strip().upper()


def scheduled_business_automation_live_active() -> bool:
    return _mode() == "LIVE" and str(os.environ.get("SCHEDULED_AUTOMATION_LIVE_ENABLED", "")).lower() in {"1", "true", "yes"}


def scheduled_business_automation_live_verified() -> bool:
    return False


def scheduled_business_automation_engineering_ready() -> bool:
    return True
