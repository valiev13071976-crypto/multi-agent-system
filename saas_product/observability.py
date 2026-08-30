"""Low-cardinality SaaS product observability."""

from __future__ import annotations


class SaaSObservability:
    def emit(self, event: str, *, metadata: dict | None = None) -> None:
        _ = event, metadata
