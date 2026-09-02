"""Controlled automation configuration."""

from __future__ import annotations

import os

MAX_CONDITION_DEPTH = 3
MAX_ACTIONS_PER_RUN = 10
MAX_EVALUATIONS_PER_HOUR = 60
DEFAULT_COOLDOWN_SECONDS = 300
MAX_CHAIN_DEPTH = 5


def _mode() -> str:
    return str(os.environ.get("CONTROLLED_AUTOMATION_MODE") or "FIXTURE").strip().upper()


def controlled_automation_expansion_live_active() -> bool:
    return _mode() == "LIVE" and str(os.environ.get("CONTROLLED_AUTOMATION_LIVE_ENABLED", "")).lower() in {"1", "true", "yes"}


def controlled_automation_expansion_live_verified() -> bool:
    return False


def controlled_automation_expansion_engineering_ready() -> bool:
    return True
