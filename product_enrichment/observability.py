"""Enrichment pipeline stage observability (requirement 14).

Mirrors the existing lightweight ``self.obs.emit(event, **meta)`` pattern
already used by ``product_intel.service._emit``/``content_intel.service``
-- no new telemetry infrastructure. ``sink`` is any object exposing
``.emit(event_type, **kwargs)`` (e.g. ``observability.runtime`` or a
domain-level observer); ``None`` is safe (best-effort, never raises).
"""

from __future__ import annotations

STAGE_IDENTITY_RESOLVED = "product_identity_resolved"
STAGE_IDENTITY_FAILED = "product_identity_failed"
STAGE_RESEARCH_STARTED = "product_research_started"
STAGE_RESEARCH_COMPLETED = "product_research_completed"
STAGE_SOURCE_ACCEPTED = "source_accepted"
STAGE_SOURCE_REJECTED = "source_rejected"
STAGE_CHARACTERISTICS_NORMALIZED = "characteristics_normalized"
STAGE_CONTENT_PREPARED = "content_prepared"
STAGE_MEDIA_DOWNLOADED = "media_downloaded"
STAGE_MEDIA_REJECTED = "media_rejected"
STAGE_MEDIA_PROCESSED = "media_processed"
STAGE_PREVIEW_READY = "enrichment_preview_ready"
STAGE_FAILED = "enrichment_failed"


def _sanitize(meta: dict) -> dict:
    # Never log secrets or binary payloads (requirement 14) -- drop bytes/
    # base64-shaped huge strings and anything literally named like a
    # credential; keep everything else (ids, urls, counts, reasons).
    out = {}
    for key, value in meta.items():
        lowered = key.casefold()
        if isinstance(value, bytes):
            continue
        if "secret" in lowered or "token" in lowered or "password" in lowered or "base64" in lowered:
            continue
        out[key] = value
    return out


class EnrichmentObserver:
    """Always records events locally (so tests never need a real sink);
    additionally forwards to an optional external sink, best-effort."""

    def __init__(self, sink=None):
        self._sink = sink
        self.events: list[dict] = []

    def emit(self, stage: str, **meta) -> None:
        safe_meta = _sanitize(meta)
        self.events.append({"stage": stage, **safe_meta})
        if self._sink is None:
            return
        try:
            self._sink.emit(stage, **safe_meta)
        except Exception:  # noqa: BLE001 -- observability must never break the pipeline
            pass

    def stages(self) -> tuple[str, ...]:
        return tuple(event["stage"] for event in self.events)
