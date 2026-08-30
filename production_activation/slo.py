"""Production SLO observation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProductionSLOObservation:
    availability: float | None = None
    latency_p95_ms: float | None = None
    error_rate: float | None = None
    throughput: float | None = None
    threshold_source: str = "stage3_stage4_evidence"

    def within_envelope(self, envelope: dict[str, Any]) -> bool:
        max_error = float(envelope.get("max_error_rate") or 0.05)
        max_p95 = float(envelope.get("max_p95_ms") or 5000)
        if self.error_rate is not None and self.error_rate > max_error:
            return False
        if self.latency_p95_ms is not None and self.latency_p95_ms > max_p95:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "availability": self.availability,
            "latency_p95_ms": self.latency_p95_ms,
            "error_rate": self.error_rate,
            "throughput": self.throughput,
            "threshold_source": self.threshold_source,
        }
