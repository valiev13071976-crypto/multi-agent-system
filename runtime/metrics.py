"""Low-cardinality runtime counters by lane (Scale 3.28+).

Never keyed by request_id / workflow_id / tenant_id.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Mapping

from task_queue.lanes import EXECUTION_LANES, normalize_lane

COUNTER_NAMES = (
    "enqueue",
    "claim",
    "complete",
    "fail",
    "retry",
    "dlq",
    "reclaim",
    "quota_reject",
    "overload_reject",
)


@dataclass
class RuntimeMetricsCounters:
    """Process-local counters: name -> lane -> count."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def inc(self, name: str, *, lane: str = "", amount: int = 1) -> None:
        key = str(name or "").strip().lower()
        if key not in COUNTER_NAMES:
            return
        lane_key = normalize_lane(lane) if lane else "unknown"
        with self._lock:
            bucket = self._counts.setdefault(key, {ln: 0 for ln in EXECUTION_LANES})
            bucket[lane_key] = int(bucket.get(lane_key, 0)) + int(amount)

    def by_lane(self, name: str) -> dict[str, int]:
        key = str(name or "").strip().lower()
        with self._lock:
            return dict(self._counts.get(key, {}))

    def total(self, name: str) -> int:
        return sum(self.by_lane(name).values())

    def as_dict(self) -> dict[str, Mapping[str, int]]:
        with self._lock:
            return {k: dict(v) for k, v in self._counts.items()}

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


RUNTIME_COUNTERS = RuntimeMetricsCounters()
