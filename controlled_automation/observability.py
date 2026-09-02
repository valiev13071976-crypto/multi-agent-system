"""Controlled automation observability."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ControlledAutomationObservability:
    events: list[dict] = field(default_factory=list)

    def emit(self, **kwargs) -> None:
        self.events.append(dict(kwargs))

    def metrics_snapshot(self, *, total: int, enabled: int, blocked: int) -> dict:
        return {
            "automations_total": total,
            "automations_enabled": enabled,
            "automations_blocked": blocked,
            "runs_succeeded": sum(1 for e in self.events if e.get("event") == "run_succeeded"),
            "runs_blocked": sum(1 for e in self.events if e.get("event") == "run_blocked"),
            "waiting_approval": sum(1 for e in self.events if e.get("event") == "waiting_approval"),
        }
