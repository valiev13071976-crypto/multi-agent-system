"""Bounded-cardinality observability counters for the artifact layer (3.5.17).

Never keyed by artifact_id / request_id / run_id / raw filename / raw
tenant_id -- only by the small, fixed label sets below. Follows the same
process-local counter pattern already used by runtime/metrics.py.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from artifacts.models import ARTIFACT_KINDS

EVENT_NAMES = (
    "artifact_upload_started",
    "artifact_upload_succeeded",
    "artifact_upload_failed",
    "artifact_registered",
    "artifact_opened",
    "artifact_downloaded",
    "artifact_download_failed",
    "artifact_access_denied",
    "artifact_generated",
    "artifact_transformation_completed",
    "artifact_deleted",
)

FAILURE_CATEGORIES = (
    "too_large",
    "type_not_allowed",
    "invalid",
    "storage_failed",
    "not_found",
    "access_denied",
    "unknown",
)

SIZE_BUCKETS = ("tiny", "small", "medium", "large", "max")


def size_bucket(size_bytes: int) -> str:
    n = max(0, int(size_bytes or 0))
    if n < 10 * 1024:
        return "tiny"
    if n < 100 * 1024:
        return "small"
    if n < 1024 * 1024:
        return "medium"
    if n < 10 * 1024 * 1024:
        return "large"
    return "max"


@dataclass
class ArtifactMetricsCounters:
    """Process-local counters: event -> bounded_label -> count."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    _by_failure: dict[str, dict[str, int]] = field(default_factory=dict)
    _by_size: dict[str, dict[str, int]] = field(default_factory=dict)

    def inc(
        self,
        event: str,
        *,
        artifact_kind: str = "unknown",
        failure_category: str = "",
        size_bytes: int | None = None,
    ) -> None:
        name = str(event or "").strip()
        if name not in EVENT_NAMES:
            return
        kind = artifact_kind if artifact_kind in ARTIFACT_KINDS else "unknown"
        with self._lock:
            bucket = self._by_kind.setdefault(name, {})
            bucket[kind] = int(bucket.get(kind, 0)) + 1
            if failure_category:
                fc = failure_category if failure_category in FAILURE_CATEGORIES else "unknown"
                fbucket = self._by_failure.setdefault(name, {})
                fbucket[fc] = int(fbucket.get(fc, 0)) + 1
            if size_bytes is not None:
                sb = size_bucket(size_bytes)
                sbucket = self._by_size.setdefault(name, {})
                sbucket[sb] = int(sbucket.get(sb, 0)) + 1

    def total(self, event: str) -> int:
        with self._lock:
            return sum(self._by_kind.get(event, {}).values())

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "by_kind": {k: dict(v) for k, v in self._by_kind.items()},
                "by_failure_category": {k: dict(v) for k, v in self._by_failure.items()},
                "by_size_bucket": {k: dict(v) for k, v in self._by_size.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._by_kind.clear()
            self._by_failure.clear()
            self._by_size.clear()


ARTIFACT_METRICS = ArtifactMetricsCounters()
