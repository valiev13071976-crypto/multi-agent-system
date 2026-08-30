"""Privacy-safe commerce observability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CommerceObservability:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, name: str, *, metadata: dict | None = None) -> None:
        safe = {}
        for k, v in (metadata or {}).items():
            if k in ("customer_name", "email", "phone", "address", "token", "secret", "raw"):
                continue
            safe[k] = v
        self.events.append((name, safe))
