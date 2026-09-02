"""Scale optimization configuration and closure flags."""

from __future__ import annotations

import os

SCHEMA_VERSION = "1"

# Bounded operational defaults (versioned policy, not magic scattered elsewhere)
MIN_SAMPLE_COUNT = 5
MIN_OBSERVATION_WINDOW_SECONDS = 30.0
SCALE_COOLDOWN_SECONDS = 120.0
SCALE_HYSTERESIS_RATIO = 0.15
MAX_LABEL_CARDINALITY = 64
FORBIDDEN_LABEL_KEYS = frozenset({
    "prompt",
    "message",
    "body",
    "email",
    "document",
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "pii",
    "raw_url",
    "content",
})

ALLOWED_LABEL_KEYS = frozenset({
    "environment",
    "service",
    "instance",
    "worker",
    "tenant_bucket",
    "workload_class",
    "queue",
    "operation",
    "provider",
    "model",
    "integration",
    "tool",
    "outcome",
    "error_class",
    "lane",
    "pool",
})

WORKLOAD_INTERACTIVE = "INTERACTIVE"
WORKLOAD_NORMAL = "NORMAL"
WORKLOAD_BATCH = "BATCH"
WORKLOAD_BACKGROUND = "BACKGROUND"
WORKLOAD_CLASSES = frozenset({WORKLOAD_INTERACTIVE, WORKLOAD_NORMAL, WORKLOAD_BATCH, WORKLOAD_BACKGROUND})


def _mode() -> str:
    return str(os.environ.get("SCALE_OPTIMIZATION_MODE") or "FIXTURE").strip().upper()


def scale_optimization_live_active() -> bool:
    return _mode() == "LIVE" and str(os.environ.get("SCALE_OPTIMIZATION_LIVE_ENABLED", "")).lower() in {
        "1",
        "true",
        "yes",
    }


def scale_optimization_live_verified() -> bool:
    return False


def scale_optimization_engineering_ready() -> bool:
    return True
