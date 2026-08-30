"""Low-cardinality admin observability."""

from __future__ import annotations

from typing import Any


class AdminObservability:
    def emit(self, event: str, *, metadata: dict[str, Any] | None = None) -> None:
        _ = event
        _ = metadata
