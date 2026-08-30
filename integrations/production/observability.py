"""Low-cardinality provider observability events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProviderObservability:
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(
        self,
        *,
        provider_id: str,
        operation: str,
        success: bool,
        latency_ms: float = 0.0,
        retry_count: int = 0,
        error_category: str = "",
        circuit_state: str = "",
        rate_limited: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "provider": provider_id,
                "operation": operation,
                "success": success,
                "latency_ms": round(latency_ms, 2),
                "retry_count": retry_count,
                "error_category": error_category,
                "circuit_state": circuit_state,
                "rate_limited": rate_limited,
                "timestamp": _utc(),
                "metadata": dict(metadata or {}),
            }
        )

    def recent(self, *, limit: int = 50) -> list[dict]:
        return list(self.events[-limit:])
