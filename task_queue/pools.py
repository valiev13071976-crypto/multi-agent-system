"""Worker pool configuration for workload isolation (Scale 3.12+)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from task_queue.lanes import (
    LANE_BACKGROUND,
    LANE_BULK,
    LANE_INTERACTIVE,
    LANE_SCHEDULED,
    normalize_lane,
    parse_worker_lanes,
)

POOL_INTERACTIVE = "interactive"
POOL_NORMAL = "normal"
POOL_BATCH = "batch"

WorkerPoolName = str  # interactive | normal | batch

POOL_NAMES = (POOL_INTERACTIVE, POOL_NORMAL, POOL_BATCH)

# Interactive pool only interactive; batch = bulk+scheduled; normal = background.
DEFAULT_POOL_LANES: dict[str, frozenset[str]] = {
    POOL_INTERACTIVE: frozenset({LANE_INTERACTIVE}),
    POOL_NORMAL: frozenset({LANE_BACKGROUND}),
    POOL_BATCH: frozenset({LANE_BULK, LANE_SCHEDULED}),
}

LANE_TO_POOL: dict[str, str] = {
    LANE_INTERACTIVE: POOL_INTERACTIVE,
    LANE_BACKGROUND: POOL_NORMAL,
    LANE_BULK: POOL_BATCH,
    LANE_SCHEDULED: POOL_BATCH,
}


@dataclass(frozen=True)
class PoolConfig:
    name: str
    allowed_lanes: frozenset[str]
    max_concurrency: int = 1

    @classmethod
    def from_env(
        cls,
        env: Mapping | None = None,
        *,
        pool_name: str | None = None,
    ) -> "PoolConfig":
        source = env if env is not None else os.environ
        name = str(pool_name or source.get("WORKER_POOL") or POOL_NORMAL).strip().lower()
        if name not in POOL_NAMES:
            # Allow lane-like aliases.
            if name in {"bulk", "scheduled"}:
                name = POOL_BATCH
            elif name in {"background", "bg"}:
                name = POOL_NORMAL
            else:
                name = POOL_NORMAL

        lanes_raw = source.get("WORKER_LANES")
        if lanes_raw is not None and str(lanes_raw).strip() and str(lanes_raw).strip().lower() not in {
            "all",
            "*",
        }:
            allowed = parse_worker_lanes(str(lanes_raw))
        else:
            allowed = DEFAULT_POOL_LANES[name]

        concurrency = 1
        raw_c = source.get("WORKER_POOL_MAX_CONCURRENCY") or source.get(
            "WORKER_MAX_CONCURRENCY"
        )
        if raw_c is not None and str(raw_c).strip():
            try:
                concurrency = max(1, int(str(raw_c).strip()))
            except ValueError:
                concurrency = 1

        return cls(name=name, allowed_lanes=frozenset(allowed), max_concurrency=concurrency)


def pool_for_lane(lane: str | None) -> str:
    """Return worker pool name for an execution lane."""

    return LANE_TO_POOL.get(normalize_lane(lane), POOL_NORMAL)


def lanes_for_pool(pool_name: str | None) -> frozenset[str]:
    name = str(pool_name or POOL_NORMAL).strip().lower()
    return DEFAULT_POOL_LANES.get(name, DEFAULT_POOL_LANES[POOL_NORMAL])
