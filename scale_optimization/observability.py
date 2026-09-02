"""Lightweight observability for scale optimization (reuses process-local pattern)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ScaleOptimizationObservability:
    events: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)

    def emit(self, event: str, **payload: Any) -> None:
        safe = {k: v for k, v in payload.items() if k.lower() not in {"prompt", "token", "password", "api_key", "body"}}
        self.events.append({"event": event, "at": datetime.now(timezone.utc).isoformat(), **safe})
        self.counters[event] = self.counters.get(event, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        return {"events": len(self.events), "counters": dict(self.counters)}
