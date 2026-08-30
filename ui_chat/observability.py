"""Low-cardinality UI Chat observability."""

from __future__ import annotations

from typing import Any


class UIChatObservability:
    def __init__(self, sink=None):
        self._sink = sink

    def emit(self, event: str, *, metadata: dict[str, Any] | None = None) -> None:
        safe = {}
        for k, v in (metadata or {}).items():
            key = str(k)
            if key in {"content", "text", "audio", "blob", "prompt", "message"}:
                continue
            safe[key] = v
        if self._sink is not None:
            try:
                self._sink(event, safe)
            except Exception:
                pass
