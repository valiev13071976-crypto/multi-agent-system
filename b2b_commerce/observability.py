"""Low-cardinality B2B observability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class B2BObservability:
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, name: str, **fields: Any) -> None:
        safe = {k: v for k, v in fields.items() if k not in {"text", "token", "price_list", "quote_body"}}
        self.events.append({"event": name, **safe})

    def last(self, name: str) -> dict[str, Any] | None:
        for item in reversed(self.events):
            if item.get("event") == name:
                return item
        return None
