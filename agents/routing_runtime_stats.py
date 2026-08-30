"""Runtime Quality / Latency / Cost signal aggregation for routing (P1.2).

Read-only, process-local bounded window of provider/model execution outcomes.
Does not own health/cooldown (see ProviderHealthTracker) and does not introduce
a weighted scoring engine. Default routing selection is unchanged unless
runtime-aware tie-breaking is explicitly enabled.

Persistence limitation: samples live in-process memory only; not shared across
workers and not durable across restarts.

Operational contract (PATCH-MR-05): ``state_scope`` is ``process_local`` and
``shared_backing`` is False. Adaptive tie-break remains OFF by default
(``DEFAULT_RUNTIME_TIEBREAK_ENABLED = False``). See readiness capabilities on
``/ready`` for the machine-visible scope signal.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Deque

from agents.routing_state_scope import STATE_SCOPE_PROCESS_LOCAL


STATS_UNKNOWN = "unknown"
STATS_INSUFFICIENT = "insufficient"
STATS_READY = "ready"
STATS_STATES = frozenset({STATS_UNKNOWN, STATS_INSUFFICIENT, STATS_READY})

# Canonical policy defaults — single source of truth (override via env).
DEFAULT_RUNTIME_STATS_WINDOW_SECONDS = 900
DEFAULT_RUNTIME_STATS_MIN_SAMPLES = 5
DEFAULT_RUNTIME_STATS_MAX_SAMPLES = 200
DEFAULT_RUNTIME_TIEBREAK_ENABLED = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RuntimeStatsPolicy:
    """Centralized bounded-window / min-sample / opt-in tie-break policy."""

    window_seconds: int = DEFAULT_RUNTIME_STATS_WINDOW_SECONDS
    min_samples: int = DEFAULT_RUNTIME_STATS_MIN_SAMPLES
    max_samples: int = DEFAULT_RUNTIME_STATS_MAX_SAMPLES
    tiebreak_enabled: bool = DEFAULT_RUNTIME_TIEBREAK_ENABLED

    def __post_init__(self):
        object.__setattr__(self, "window_seconds", max(1, int(self.window_seconds)))
        object.__setattr__(self, "min_samples", max(1, int(self.min_samples)))
        object.__setattr__(
            self, "max_samples", max(self.min_samples, int(self.max_samples))
        )
        object.__setattr__(self, "tiebreak_enabled", bool(self.tiebreak_enabled))


@dataclass(frozen=True)
class ProviderRuntimeStats:
    """Immutable routing-safe runtime performance snapshot."""

    provider_id: str
    model_id: str
    sample_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    success_rate: float | None = None
    latency_avg_ms: float | None = None
    latency_p95_ms: float | None = None
    cost_avg: Decimal | None = None
    last_updated: datetime | None = None
    window_seconds: int = DEFAULT_RUNTIME_STATS_WINDOW_SECONDS
    state: str = STATS_UNKNOWN

    def __post_init__(self):
        if self.state not in STATS_STATES:
            raise ValueError(f"invalid_runtime_stats_state:{self.state}")
        object.__setattr__(self, "provider_id", str(self.provider_id or ""))
        object.__setattr__(self, "model_id", str(self.model_id or ""))
        object.__setattr__(self, "sample_count", int(self.sample_count))
        object.__setattr__(self, "success_count", int(self.success_count))
        object.__setattr__(self, "failure_count", int(self.failure_count))
        object.__setattr__(self, "window_seconds", int(self.window_seconds))
        if self.cost_avg is not None and not isinstance(self.cost_avg, Decimal):
            object.__setattr__(self, "cost_avg", Decimal(str(self.cost_avg)))
        stamp = self.last_updated
        if stamp is not None and stamp.tzinfo is None:
            object.__setattr__(
                self, "last_updated", stamp.replace(tzinfo=timezone.utc)
            )

    @property
    def usable(self) -> bool:
        return self.state == STATS_READY

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "sample_count": self.sample_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": self.success_rate,
            "latency_avg_ms": self.latency_avg_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "cost_avg": str(self.cost_avg) if self.cost_avg is not None else None,
            "last_updated": (
                self.last_updated.isoformat() if self.last_updated is not None else None
            ),
            "window_seconds": self.window_seconds,
            "state": self.state,
        }


@dataclass(frozen=True)
class _RuntimeSample:
    kind: str  # success | failure
    timestamp: datetime
    latency_ms: float | None = None
    cost: Decimal | None = None


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(raw: str | None, default: int) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def load_runtime_stats_policy(*, env: dict | None = None) -> RuntimeStatsPolicy:
    source = env if env is not None else os.environ
    return RuntimeStatsPolicy(
        window_seconds=_parse_int(
            source.get("ROUTING_RUNTIME_STATS_WINDOW_SECONDS"),
            DEFAULT_RUNTIME_STATS_WINDOW_SECONDS,
        ),
        min_samples=_parse_int(
            source.get("ROUTING_RUNTIME_STATS_MIN_SAMPLES"),
            DEFAULT_RUNTIME_STATS_MIN_SAMPLES,
        ),
        max_samples=_parse_int(
            source.get("ROUTING_RUNTIME_STATS_MAX_SAMPLES"),
            DEFAULT_RUNTIME_STATS_MAX_SAMPLES,
        ),
        tiebreak_enabled=_parse_bool(
            source.get("ROUTING_RUNTIME_TIEBREAK_ENABLED"),
            DEFAULT_RUNTIME_TIEBREAK_ENABLED,
        ),
    )


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    return float(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)


class ProviderRuntimeStatsAggregator:
    """Process-local bounded aggregator for routing-safe runtime metrics.

    Implements ``ProviderRuntimeStatsStore``. Independent instances do not share
    samples across workers.
    """

    STATE_SCOPE = STATE_SCOPE_PROCESS_LOCAL

    def __init__(self, policy: RuntimeStatsPolicy | None = None):
        self.policy = policy or RuntimeStatsPolicy()
        self._samples: dict[tuple[str, str], Deque[_RuntimeSample]] = {}
        self._lock = threading.Lock()

    @property
    def state_scope(self) -> str:
        return self.STATE_SCOPE

    @property
    def shared_backing(self) -> bool:
        return False

    def _key(self, provider_id: str, model_id: str = "") -> tuple[str, str]:
        return (str(provider_id or ""), str(model_id or ""))

    def _prune(self, key: tuple[str, str], now: datetime) -> None:
        window = timedelta(seconds=self.policy.window_seconds)
        bucket = self._samples.get(key)
        if not bucket:
            return
        while bucket and (now - bucket[0].timestamp) > window:
            bucket.popleft()
        while len(bucket) > self.policy.max_samples:
            bucket.popleft()

    def record_success(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        latency_ms: float | None = None,
        cost: Decimal | None = None,
        now: datetime | None = None,
    ) -> ProviderRuntimeStats:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        cost_value = None if cost is None else Decimal(str(cost))
        latency_value = None if latency_ms is None else float(latency_ms)
        key = self._key(provider_id, model_id)
        with self._lock:
            bucket = self._samples.setdefault(key, deque())
            bucket.append(
                _RuntimeSample(
                    kind="success",
                    timestamp=stamp,
                    latency_ms=latency_value,
                    cost=cost_value,
                )
            )
            self._prune(key, stamp)
        return self.snapshot(provider_id, model_id, now=stamp)

    def record_failure(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        latency_ms: float | None = None,
        now: datetime | None = None,
    ) -> ProviderRuntimeStats:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        latency_value = None if latency_ms is None else float(latency_ms)
        key = self._key(provider_id, model_id)
        with self._lock:
            bucket = self._samples.setdefault(key, deque())
            bucket.append(
                _RuntimeSample(
                    kind="failure",
                    timestamp=stamp,
                    latency_ms=latency_value,
                    cost=None,
                )
            )
            self._prune(key, stamp)
        return self.snapshot(provider_id, model_id, now=stamp)

    def snapshot(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now: datetime | None = None,
    ) -> ProviderRuntimeStats:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        key = self._key(provider_id, model_id)
        with self._lock:
            self._prune(key, stamp)
            samples = tuple(self._samples.get(key, ()))

        if not samples:
            return ProviderRuntimeStats(
                provider_id=provider_id,
                model_id=model_id,
                window_seconds=self.policy.window_seconds,
                state=STATS_UNKNOWN,
            )

        success_count = sum(1 for s in samples if s.kind == "success")
        failure_count = sum(1 for s in samples if s.kind == "failure")
        sample_count = len(samples)
        success_rate = success_count / sample_count if sample_count else None

        latencies = sorted(
            s.latency_ms for s in samples if s.latency_ms is not None
        )
        latency_avg = (
            sum(latencies) / len(latencies) if latencies else None
        )
        latency_p95 = _percentile(latencies, 0.95) if latencies else None

        costs = [s.cost for s in samples if s.cost is not None]
        cost_avg = (
            sum(costs, Decimal("0")) / Decimal(len(costs)) if costs else None
        )

        last_updated = max(s.timestamp for s in samples)
        state = (
            STATS_READY
            if sample_count >= self.policy.min_samples
            else STATS_INSUFFICIENT
        )
        return ProviderRuntimeStats(
            provider_id=provider_id,
            model_id=model_id,
            sample_count=sample_count,
            success_count=success_count,
            failure_count=failure_count,
            success_rate=success_rate,
            latency_avg_ms=latency_avg,
            latency_p95_ms=latency_p95,
            cost_avg=cost_avg,
            last_updated=last_updated,
            window_seconds=self.policy.window_seconds,
            state=state,
        )
