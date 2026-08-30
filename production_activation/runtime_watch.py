"""Provider and runtime stability watch."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProductionRuntimeWatch:
    queue_depth: int = 0
    dlq_growth: int = 0
    provider_unhealthy: int = 0
    circuit_open: int = 0
    interactive_p95_ms: float | None = None

    def record(
        self,
        *,
        queue_depth: int | None = None,
        dlq_growth: int | None = None,
        provider_unhealthy: int | None = None,
        circuit_open: int | None = None,
        interactive_p95_ms: float | None = None,
    ) -> None:
        if queue_depth is not None:
            self.queue_depth = queue_depth
        if dlq_growth is not None:
            self.dlq_growth = dlq_growth
        if provider_unhealthy is not None:
            self.provider_unhealthy = provider_unhealthy
        if circuit_open is not None:
            self.circuit_open = circuit_open
        if interactive_p95_ms is not None:
            self.interactive_p95_ms = interactive_p95_ms

    def exceeds_envelope(self, envelope: dict) -> bool:
        max_queue = int(envelope.get("max_queue_depth") or 0)
        if max_queue and self.queue_depth > max_queue:
            return True
        return self.provider_unhealthy > 0

    def as_dict(self) -> dict:
        return {
            "queue_depth": self.queue_depth,
            "dlq_growth": self.dlq_growth,
            "provider_unhealthy": self.provider_unhealthy,
            "circuit_open": self.circuit_open,
            "interactive_p95_ms": self.interactive_p95_ms,
        }
