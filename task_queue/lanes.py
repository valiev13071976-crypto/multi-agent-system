"""Workload classification and execution-lane helpers (Phase 3 Block 2 / Scale).

Security note
-------------
User-controlled request payloads must **not** set privileged priority / lane /
workload without trusted metadata keys. Prefer classifying via
``classify_workload`` using only:

- ``trusted_job_type``
- ``workload_class``
- ``execution_lane``

Untrusted body fields (e.g. client-supplied ``priority`` or ``lane`` inside
payload JSON) are ignored unless the caller maps them through a trusted
control plane first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from task_queue.models import (
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PRIORITY_RANK,
    utc_now,
)

LANE_INTERACTIVE = "interactive"
LANE_BACKGROUND = "background"
LANE_BULK = "bulk"
LANE_SCHEDULED = "scheduled"

EXECUTION_LANES = (
    LANE_INTERACTIVE,
    LANE_BACKGROUND,
    LANE_BULK,
    LANE_SCHEDULED,
)

# Non-interactive lanes share background capacity pool.
BACKGROUND_POOL_LANES = frozenset({LANE_BACKGROUND, LANE_BULK, LANE_SCHEDULED})

DEFAULT_LANE = LANE_BACKGROUND

# Workload classes (Scale 3.12+) — map onto execution lanes.
WORKLOAD_INTERACTIVE = "interactive"
WORKLOAD_NORMAL = "normal"
WORKLOAD_BATCH = "batch"
WORKLOAD_BACKGROUND = "background"

WORKLOAD_CLASSES = (
    WORKLOAD_INTERACTIVE,
    WORKLOAD_NORMAL,
    WORKLOAD_BATCH,
    WORKLOAD_BACKGROUND,
)

WORKLOAD_TO_LANE = {
    WORKLOAD_INTERACTIVE: LANE_INTERACTIVE,
    WORKLOAD_NORMAL: LANE_BACKGROUND,
    WORKLOAD_BATCH: LANE_BULK,
    WORKLOAD_BACKGROUND: LANE_BACKGROUND,
}

# Trusted metadata keys only — never privilege from raw user payload.
TRUSTED_WORKLOAD_META_KEYS = frozenset(
    {"trusted_job_type", "workload_class", "execution_lane"}
)

HEAVY_JOB_TYPES_BATCH = frozenset(
    {
        "excel_large",
        "crawler",
        "scraping",
        "document_ocr",
        "document_large",
        "document_bulk",
        "data_large",
        "knowledge_large",
        "content_large",
        "content_bulk",
        "media_large",
        "media_bulk",
        "commerce_large",
        "commerce_bulk",
    }
)
HEAVY_JOB_TYPES_BACKGROUND = frozenset({"media", "content_generation"})
HEAVY_JOB_TYPES = HEAVY_JOB_TYPES_BATCH | HEAVY_JOB_TYPES_BACKGROUND

DEFAULT_LARGE_EXCEL_ROWS = 100_000
DEFAULT_LARGE_DATA_BYTES = 50 * 1024 * 1024  # 50MB


@dataclass(frozen=True)
class WorkloadClass:
    """Named workload class with resolved execution lane."""

    name: str
    lane: str

    def __str__(self) -> str:
        return self.name


def normalize_workload(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in WORKLOAD_CLASSES:
        return text
    aliases = {
        "interactive": WORKLOAD_INTERACTIVE,
        "user": WORKLOAD_INTERACTIVE,
        "foreground": WORKLOAD_INTERACTIVE,
        "normal": WORKLOAD_NORMAL,
        "default": WORKLOAD_NORMAL,
        "batch": WORKLOAD_BATCH,
        "bulk": WORKLOAD_BATCH,
        "background": WORKLOAD_BACKGROUND,
        "bg": WORKLOAD_BACKGROUND,
        "scheduled": WORKLOAD_BATCH,  # scheduled work shares batch pool
    }
    return aliases.get(text, WORKLOAD_NORMAL)


def normalize_lane(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in EXECUTION_LANES:
        return text
    aliases = {
        "interactive": LANE_INTERACTIVE,
        "user": LANE_INTERACTIVE,
        "foreground": LANE_INTERACTIVE,
        "background": LANE_BACKGROUND,
        "bg": LANE_BACKGROUND,
        "normal": LANE_BACKGROUND,  # workload alias → background lane
        "bulk": LANE_BULK,
        "batch": LANE_BULK,
        "scheduled": LANE_SCHEDULED,
        "schedule": LANE_SCHEDULED,
        "cron": LANE_SCHEDULED,
    }
    return aliases.get(text, DEFAULT_LANE)


def is_interactive_lane(lane: str | None) -> bool:
    return normalize_lane(lane) == LANE_INTERACTIVE


def workload_to_lane(workload: str | None) -> str:
    return WORKLOAD_TO_LANE.get(normalize_workload(workload), DEFAULT_LANE)


def large_excel_rows_threshold(env: Mapping | None = None) -> int:
    source = env if env is not None else os.environ
    raw = source.get("LARGE_EXCEL_ROWS")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LARGE_EXCEL_ROWS
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return DEFAULT_LARGE_EXCEL_ROWS


def large_data_bytes_threshold(env: Mapping | None = None) -> int:
    source = env if env is not None else os.environ
    raw = source.get("LARGE_DATA_BYTES")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LARGE_DATA_BYTES
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return DEFAULT_LARGE_DATA_BYTES


def route_heavy_job(job_type: str | None) -> str:
    """Map heavy job types to an execution lane (no Excel/crawler impl)."""

    jt = str(job_type or "").strip().lower()
    if jt in HEAVY_JOB_TYPES_BATCH:
        return LANE_BULK
    if jt in HEAVY_JOB_TYPES_BACKGROUND:
        return LANE_BACKGROUND
    return DEFAULT_LANE


def _trusted_meta_value(meta: Mapping, *keys: str) -> str | None:
    for key in keys:
        if key not in TRUSTED_WORKLOAD_META_KEYS:
            continue
        if key in meta and meta.get(key) not in (None, ""):
            return str(meta.get(key))
    return None


def classify_workload(
    *,
    priority: str | None = None,
    metadata: Mapping | None = None,
    job_type: str | None = None,
    estimated_bytes: int | None = None,
    estimated_rows: int | None = None,
    env: Mapping | None = None,
) -> WorkloadClass:
    """Classify workload using TRUSTED metadata only for privileged lane/class.

    Trusted keys: ``trusted_job_type``, ``workload_class``, ``execution_lane``.
    Size thresholds: LARGE_EXCEL_ROWS (default 100000), LARGE_DATA_BYTES (50MB).
    """

    meta = dict(metadata or {})
    # Explicit trusted class / lane first.
    trusted_class = _trusted_meta_value(meta, "workload_class")
    if trusted_class:
        name = normalize_workload(trusted_class)
        return WorkloadClass(name=name, lane=workload_to_lane(name))

    trusted_lane = _trusted_meta_value(meta, "execution_lane")
    if trusted_lane:
        lane = normalize_lane(trusted_lane)
        # Map lane back to nearest workload class.
        if lane == LANE_INTERACTIVE:
            return WorkloadClass(WORKLOAD_INTERACTIVE, LANE_INTERACTIVE)
        if lane == LANE_BULK:
            return WorkloadClass(WORKLOAD_BATCH, LANE_BULK)
        if lane == LANE_SCHEDULED:
            return WorkloadClass(WORKLOAD_BATCH, LANE_SCHEDULED)
        return WorkloadClass(WORKLOAD_NORMAL, LANE_BACKGROUND)

    trusted_jt = job_type or _trusted_meta_value(meta, "trusted_job_type")
    if trusted_jt:
        jt = str(trusted_jt).strip().lower()
        if jt in HEAVY_JOB_TYPES_BATCH:
            return WorkloadClass(WORKLOAD_BATCH, LANE_BULK)
        if jt in HEAVY_JOB_TYPES_BACKGROUND:
            return WorkloadClass(WORKLOAD_BACKGROUND, LANE_BACKGROUND)

    rows = estimated_rows
    if rows is None and meta.get("estimated_rows") is not None:
        try:
            rows = int(meta.get("estimated_rows"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            rows = None
    nbytes = estimated_bytes
    if nbytes is None and meta.get("estimated_bytes") is not None:
        try:
            nbytes = int(meta.get("estimated_bytes"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            nbytes = None

    if rows is not None and rows >= large_excel_rows_threshold(env):
        return WorkloadClass(WORKLOAD_BATCH, LANE_BULK)
    if nbytes is not None and nbytes >= large_data_bytes_threshold(env):
        return WorkloadClass(WORKLOAD_BATCH, LANE_BULK)

    trigger = str(meta.get("trigger") or "").lower()
    if trigger == "scheduled":
        return WorkloadClass(WORKLOAD_BATCH, LANE_SCHEDULED)

    if priority in {PRIORITY_HIGH, PRIORITY_CRITICAL}:
        return WorkloadClass(WORKLOAD_INTERACTIVE, LANE_INTERACTIVE)
    if priority == PRIORITY_LOW:
        return WorkloadClass(WORKLOAD_BACKGROUND, LANE_BACKGROUND)
    return WorkloadClass(WORKLOAD_NORMAL, LANE_BACKGROUND)


def resolve_execution_lane(
    *,
    execution_lane: str | None = None,
    workload_class: str | None = None,
    priority: str | None = None,
    metadata: Mapping | None = None,
) -> str:
    """Resolve first-class lane from explicit args / metadata / heuristics."""

    meta = dict(metadata or {})
    explicit = execution_lane or meta.get("execution_lane") or meta.get("workload_class")
    if explicit:
        return normalize_lane(str(explicit))
    if workload_class:
        return normalize_lane(str(workload_class))
    classified = classify_workload(priority=priority, metadata=meta)
    return classified.lane


def parse_worker_lanes(raw: str | None) -> frozenset[str]:
    """WORKER_LANES=all|interactive|background,bulk,scheduled|normal"""

    text = str(raw or "all").strip().lower()
    if not text or text == "all" or text == "*":
        return frozenset(EXECUTION_LANES)
    parts = {normalize_lane(p.strip()) for p in text.split(",") if p.strip()}
    return frozenset(parts) or frozenset(EXECUTION_LANES)


@dataclass(frozen=True)
class LaneCapacityConfig:
    """Interactive reservation + optional controlled borrow."""

    interactive_reserved: int = 5
    background_may_borrow: bool = True
    aging_seconds_per_step: float = 60.0
    aging_max_boost: int = 2  # max priority-rank boost (does not change lane)
    fairness_enabled: bool = True

    @classmethod
    def from_env(cls, env: Mapping | None = None) -> "LaneCapacityConfig":
        source = env if env is not None else os.environ

        def _int(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                return max(0, int(str(raw).strip()))
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                return max(0.0, float(str(raw).strip()))
            except ValueError:
                return default

        def _bool(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            interactive_reserved=_int("INTERACTIVE_RESERVED", 5),
            background_may_borrow=_bool("BACKGROUND_MAY_BORROW_INTERACTIVE", True),
            aging_seconds_per_step=_float("PRIORITY_AGING_SECONDS", 60.0),
            aging_max_boost=_int("PRIORITY_AGING_MAX_BOOST", 2),
            fairness_enabled=_bool("TENANT_FAIRNESS_ENABLED", True),
        )


def effective_priority_rank(
    priority: str,
    *,
    created_at: datetime,
    now: datetime | None = None,
    aging_seconds_per_step: float = 60.0,
    aging_max_boost: int = 2,
) -> int:
    """Bounded aging within lane — never promotes into another lane."""

    base = int(PRIORITY_RANK.get(priority, PRIORITY_RANK[PRIORITY_NORMAL]))
    if aging_seconds_per_step <= 0 or aging_max_boost <= 0:
        return base
    stamp = now or utc_now()
    age = max(0.0, (stamp - created_at).total_seconds())
    steps = int(age // float(aging_seconds_per_step))
    boost = min(int(aging_max_boost), steps)
    # Cap at critical rank so aging cannot invent a super-priority class.
    return min(PRIORITY_RANK[PRIORITY_CRITICAL], base + boost)
