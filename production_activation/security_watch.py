"""Production security watch."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProductionSecurityWatch:
    events: list[str] = field(default_factory=list)
    p0_count: int = 0
    p1_count: int = 0

    def record(self, signal: str, *, severity: str = "P1") -> None:
        self.events.append(signal)
        if severity == "P0":
            self.p0_count += 1
        elif severity == "P1":
            self.p1_count += 1

    def blocks_acceptance(self) -> bool:
        return self.p0_count > 0 or self.p1_count > 0

    def as_dict(self) -> dict:
        return {"events": list(self.events), "p0_count": self.p0_count, "p1_count": self.p1_count}
