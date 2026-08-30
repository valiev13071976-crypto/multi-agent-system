"""Data Intelligence observability — sanitized lifecycle events."""

from __future__ import annotations

from typing import Callable, Mapping

_REDACT_KEYS = frozenset(
    {
        "rows",
        "raw",
        "content",
        "cells",
        "table",
        "tables",
        "inn",
        "payment",
        "price_list",
        "counterparty",
        "values",
        "payload",
    }
)


def sanitize_event_payload(payload: Mapping[str, object] | None) -> dict:
    out: dict = {}
    for key, value in dict(payload or {}).items():
        if str(key).lower() in _REDACT_KEYS:
            continue
        if isinstance(value, str) and len(value) > 256:
            out[key] = f"<redacted len={len(value)}>"
        else:
            out[key] = value
    return out


class DataIntelObserver:
    def __init__(self):
        self._sinks: list[Callable[[str, dict], None]] = []

    def add_sink(self, sink: Callable[[str, dict], None]) -> None:
        self._sinks.append(sink)

    def emit(self, event: str, **payload) -> None:
        safe = sanitize_event_payload(payload)
        for sink in self._sinks:
            try:
                sink(event, safe)
            except Exception:
                pass

    def on_ingested(self, **kw) -> None:
        self.emit("dataset.ingested", **kw)

    def on_structure_detected(self, **kw) -> None:
        self.emit("dataset.structure_detected", **kw)

    def on_normalized(self, **kw) -> None:
        self.emit("dataset.normalized", **kw)

    def on_matched(self, **kw) -> None:
        self.emit("dataset.matched", **kw)

    def on_duplicates(self, **kw) -> None:
        self.emit("dataset.duplicates.detected", **kw)

    def on_price_comparison(self, **kw) -> None:
        self.emit("dataset.price_comparison.completed", **kw)

    def on_reconciled(self, **kw) -> None:
        self.emit("dataset.reconciliation.completed", **kw)

    def on_merge(self, **kw) -> None:
        self.emit("dataset.merge.completed", **kw)

    def on_analytics(self, **kw) -> None:
        self.emit("dataset.analytics.completed", **kw)

    def on_generated(self, **kw) -> None:
        self.emit("dataset.generated", **kw)

    def on_checkpoint(self, **kw) -> None:
        self.emit("dataset.checkpointed", **kw)

    def on_failed(self, **kw) -> None:
        self.emit("dataset.failed", **kw)
