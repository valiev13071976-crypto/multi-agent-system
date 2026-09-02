"""Analytics dashboard configuration."""

from __future__ import annotations

import os

MAX_TIME_RANGE_DAYS = 366
MAX_TIMESERIES_POINTS = 366
DEFAULT_TIMEZONE = "Europe/Moscow"
MAX_DRILLDOWN_LIMIT = 100


def _mode() -> str:
    return str(os.environ.get("ANALYTICS_DASHBOARD_MODE") or "FIXTURE").strip().upper()


def analytics_dashboard_live_active() -> bool:
    return _mode() == "LIVE" and bool(os.environ.get("ANALYTICS_DASHBOARD_LIVE_URL"))


def analytics_dashboard_live_verified() -> bool:
    return False


def analytics_dashboard_engineering_ready() -> bool:
    return True
