"""Scheduled automation observability."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScheduleObservability:
    events: list[dict] = field(default_factory=list)

    def emit(self, **kwargs) -> None:
        self.events.append(dict(kwargs))

    def metrics_snapshot(self, *, schedules_total: int, schedules_enabled: int, due: int) -> dict:
        return {
            "schedules_total": schedules_total,
            "schedules_enabled": schedules_enabled,
            "due_occurrences": due,
            "dispatch_success": sum(1 for e in self.events if e.get("event") == "dispatch_success"),
            "dispatch_failure": sum(1 for e in self.events if e.get("event") == "dispatch_failure"),
            "duplicate_dispatch_prevented": sum(1 for e in self.events if e.get("event") == "duplicate_prevented"),
        }
