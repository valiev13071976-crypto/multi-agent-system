"""Sanitized content lifecycle events."""

from __future__ import annotations

from autonomy.models import sanitize_metadata


class ContentObservability:
    def __init__(self, runtime=None):
        self._runtime = runtime

    def emit(self, event_type: str, *, status: str = "", metadata: dict | None = None) -> None:
        obs = self._runtime
        if obs is None:
            return
        safe = sanitize_metadata(
            {
                k: v
                for k, v in dict(metadata or {}).items()
                if k
                not in {
                    "body",
                    "content",
                    "script",
                    "strategy",
                    "prompt",
                    "evidence_text",
                    "raw",
                }
            }
        )
        try:
            obs.emit(event_type, component="content_intel", status=status, metadata=safe)
        except Exception:
            pass
