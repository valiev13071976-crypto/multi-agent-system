"""SEO observability — privacy-safe events."""

from __future__ import annotations

_REDACT_KEYS = frozenset(
    {
        "token",
        "secret",
        "authorization",
        "html",
        "body",
        "oauth",
        "credential",
        "query_corpus",
    }
)


class SeoObservability:
    def __init__(self, backend=None):
        self._backend = backend
        self.events: list[dict] = []

    def emit(self, name: str, *, status: str = "ok", metadata: dict | None = None) -> None:
        meta = {}
        for key, value in dict(metadata or {}).items():
            if str(key).lower() in _REDACT_KEYS:
                continue
            if isinstance(value, str) and len(value) > 200:
                meta[key] = f"<redacted len={len(value)}>"
            else:
                meta[key] = value
        event = {"event": name, "status": status, "metadata": meta}
        self.events.append(event)
        if self._backend is not None and hasattr(self._backend, "emit"):
            try:
                self._backend.emit(name, status=status, metadata=meta)
            except Exception:
                pass
