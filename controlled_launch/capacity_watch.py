"""Runtime / capacity watch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CapacityWatch:
    queue_depth: int = 0
    worker_utilization: float = 0.0
    interactive_p95_ms: float | None = None
    rejections: int = 0
    dlq_growth: int = 0
    envelope: dict[str, Any] = field(default_factory=dict)

    def record(
        self,
        *,
        queue_depth: int | None = None,
        worker_utilization: float | None = None,
        interactive_p95_ms: float | None = None,
        rejections: int | None = None,
        dlq_growth: int | None = None,
    ) -> None:
        if queue_depth is not None:
            self.queue_depth = queue_depth
        if worker_utilization is not None:
            self.worker_utilization = worker_utilization
        if interactive_p95_ms is not None:
            self.interactive_p95_ms = interactive_p95_ms
        if rejections is not None:
            self.rejections = rejections
        if dlq_growth is not None:
            self.dlq_growth = dlq_growth

    def exceeds_envelope(self) -> bool:
        max_queue = int(self.envelope.get("max_queue_depth") or 0)
        max_p95 = float(self.envelope.get("max_interactive_p95_ms") or 0)
        if max_queue and self.queue_depth > max_queue:
            return True
        if max_p95 and self.interactive_p95_ms is not None and self.interactive_p95_ms > max_p95:
            return True
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue_depth": self.queue_depth,
            "worker_utilization": self.worker_utilization,
            "interactive_p95_ms": self.interactive_p95_ms,
            "rejections": self.rejections,
            "dlq_growth": self.dlq_growth,
            "envelope": dict(self.envelope),
            "exceeds_envelope": self.exceeds_envelope(),
        }
