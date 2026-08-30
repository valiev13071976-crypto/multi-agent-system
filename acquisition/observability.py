"""Acquisition observability — lifecycle events, low-cardinality metrics, no secrets/content."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

_REDACT_KEYS = frozenset(
    {
        "secret",
        "password",
        "token",
        "api_key",
        "authorization",
        "credential",
        "body",
        "body_text",
        "content",
        "content_text",
        "html",
        "raw",
    }
)


def sanitize_event_payload(payload: dict | None) -> dict:
    out = {}
    for key, value in dict(payload or {}).items():
        lk = str(key).lower()
        if any(s in lk for s in _REDACT_KEYS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            # Bound string cardinality / size
            if isinstance(value, str) and len(value) > 256:
                out[key] = value[:256]
            else:
                out[key] = value
        elif isinstance(value, (list, tuple)):
            out[key] = list(value)[:32]
        elif isinstance(value, dict):
            out[key] = sanitize_event_payload(value)
    return out


@dataclass
class AcquisitionMetrics:
    jobs_submitted: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    jobs_cancelled: int = 0
    pages_fetched: int = 0
    pages_failed: int = 0
    pages_skipped: int = 0
    records_normalized: int = 0
    records_deduped: int = 0
    ingest_accepted: int = 0
    ingest_duplicate: int = 0
    capacity_rejected: int = 0
    policy_denied: int = 0

    def as_dict(self) -> dict:
        return {
            "jobs_submitted": self.jobs_submitted,
            "jobs_completed": self.jobs_completed,
            "jobs_failed": self.jobs_failed,
            "jobs_cancelled": self.jobs_cancelled,
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "pages_skipped": self.pages_skipped,
            "records_normalized": self.records_normalized,
            "records_deduped": self.records_deduped,
            "ingest_accepted": self.ingest_accepted,
            "ingest_duplicate": self.ingest_duplicate,
            "capacity_rejected": self.capacity_rejected,
            "policy_denied": self.policy_denied,
        }


@dataclass
class AcquisitionObserver:
    metrics: AcquisitionMetrics = field(default_factory=AcquisitionMetrics)
    _sinks: list[Callable[[str, dict], None]] = field(default_factory=list)

    def add_sink(self, sink: Callable[[str, dict], None]) -> None:
        self._sinks.append(sink)

    def emit(self, event: str, payload: dict | None = None) -> None:
        safe = sanitize_event_payload(payload)
        safe.setdefault("event", event)
        for sink in self._sinks:
            try:
                sink(event, safe)
            except Exception:
                pass
        try:
            from observability.events import emit_event

            emit_event(event, safe)
        except Exception:
            pass

    def on_job_submitted(self, *, job_id: str, tenant_id: str, mode: str, lane: str) -> None:
        self.metrics.jobs_submitted += 1
        self.emit(
            "acquisition.job.submitted",
            {"job_id": job_id, "tenant_id": tenant_id, "mode": mode, "lane": lane},
        )

    def on_job_completed(self, *, job_id: str, tenant_id: str, status: str) -> None:
        if status == "cancelled":
            self.metrics.jobs_cancelled += 1
        elif status in {"failed", "rejected"}:
            self.metrics.jobs_failed += 1
        else:
            self.metrics.jobs_completed += 1
        self.emit(
            "acquisition.job.completed",
            {"job_id": job_id, "tenant_id": tenant_id, "status": status},
        )

    def on_pages(self, *, fetched: int = 0, failed: int = 0, skipped: int = 0) -> None:
        self.metrics.pages_fetched += fetched
        self.metrics.pages_failed += failed
        self.metrics.pages_skipped += skipped

    def on_capacity_rejected(self, *, tenant_id: str, reason: str) -> None:
        self.metrics.capacity_rejected += 1
        self.emit(
            "acquisition.capacity.rejected",
            {"tenant_id": tenant_id, "reason": reason},
        )

    def on_policy_denied(self, *, tenant_id: str, reason: str) -> None:
        self.metrics.policy_denied += 1
        self.emit(
            "acquisition.policy.denied",
            {"tenant_id": tenant_id, "reason": reason},
        )

    def on_retry_scheduled(self, *, reason: str, retry_count: int) -> None:
        self.emit(
            "acquisition.retry.scheduled",
            {"reason": reason, "retry_count": int(retry_count)},
        )

    def on_retry_exhausted(self, *, reason: str, retry_count: int) -> None:
        self.emit(
            "acquisition.retry.exhausted",
            {"reason": reason, "retry_count": int(retry_count)},
        )


_DEFAULT = AcquisitionObserver()


def get_observer() -> AcquisitionObserver:
    return _DEFAULT
