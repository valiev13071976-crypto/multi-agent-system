"""Rollout security watch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SecurityWatch:
    events: list[str] = field(default_factory=list)
    p0_count: int = 0
    p1_count: int = 0

    def record(self, signal: str, *, severity: str = "P1") -> None:
        self.events.append(signal)
        if severity == "P0":
            self.p0_count += 1
        elif severity == "P1":
            self.p1_count += 1

    def as_dict(self) -> dict[str, Any]:
        return {"events": list(self.events), "p0_count": self.p0_count, "p1_count": self.p1_count}

    def blocks_expansion(self) -> bool:
        return self.p0_count > 0 or self.p1_count > 0
