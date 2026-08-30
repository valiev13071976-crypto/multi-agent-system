"""Dynamic provider/model health for auto routing (P1.1).

Process-local, deterministic cooldown based on recent operational failures.
Does not use eval scores or answer-quality signals.

Persistence limitation: health state lives in-process memory only; it does not
survive process restart and is not shared across workers.

Operational contract (PATCH-MR-05): ``state_scope`` is ``process_local`` and
``shared_backing`` is False unless a future shared store is plugged in via
``agents.routing_state_scope.ProviderHealthStore``. See readiness capabilities
on ``/ready`` for the machine-visible signal.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque

from agents.routing_state_scope import STATE_SCOPE_PROCESS_LOCAL


HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_COOLDOWN = "cooldown"
HEALTH_UNKNOWN = "unknown"
HEALTH_STATES = frozenset(
    {HEALTH_HEALTHY, HEALTH_DEGRADED, HEALTH_COOLDOWN, HEALTH_UNKNOWN}
)

REASON_COOLDOWN_ACTIVE = "health_cooldown"
REASON_REPEATED_FAILURES = "repeated_provider_failures"
REASON_INSUFFICIENT_SAMPLES = "insufficient_samples"

# Canonical policy defaults — single source of truth (override via env).
DEFAULT_HEALTH_WINDOW_SECONDS = 300
DEFAULT_HEALTH_FAILURE_THRESHOLD = 3
DEFAULT_HEALTH_COOLDOWN_SECONDS = 60
DEFAULT_HEALTH_ENABLED = True

# Exception type-name markers that count as provider operational failures.
_QUALIFYING_FAILURE_MARKERS = (
    "Timeout",
    "TimeoutError",
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectError",
    "ConnectionError",
    "NetworkError",
    "RemoteProtocolError",
    "HTTPStatusError",
    "InternalServerError",
    "ServiceUnavailable",
    "APIConnectionError",
    "APITimeoutError",
    "APIError",
    "ProviderError",
    "RateLimitError",  # repeated provider-side pressure
)

# Error / exception names that must never affect routing health.
_IGNORED_FAILURE_MARKERS = (
    "FinOpsBudgetDenied",
    "BudgetRoutingDenied",
    "BudgetGuard",
    "ProviderCapabilityMismatch",
    "NoCapableProvider",
    "InvalidMode",
    "InvalidRole",
    "ProviderNotConfigured",
    "WaitingApproval",
    "ValidationError",
    "HTTPException",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RoutingHealthPolicy:
    """Centralized deterministic health/cooldown policy."""

    window_seconds: int = DEFAULT_HEALTH_WINDOW_SECONDS
    failure_threshold: int = DEFAULT_HEALTH_FAILURE_THRESHOLD
    cooldown_seconds: int = DEFAULT_HEALTH_COOLDOWN_SECONDS
    enabled: bool = DEFAULT_HEALTH_ENABLED

    def __post_init__(self):
        object.__setattr__(self, "window_seconds", max(1, int(self.window_seconds)))
        object.__setattr__(
            self, "failure_threshold", max(1, int(self.failure_threshold))
        )
        object.__setattr__(self, "cooldown_seconds", max(1, int(self.cooldown_seconds)))
        object.__setattr__(self, "enabled", bool(self.enabled))


@dataclass(frozen=True)
class ProviderHealthSnapshot:
    """Immutable routing-facing health view for one provider/model."""

    provider_id: str
    model_id: str
    state: str
    recent_failure_count: int = 0
    recent_success_count: int = 0
    cooldown_until: datetime | None = None
    reason_code: str | None = None
    window_seconds: int = DEFAULT_HEALTH_WINDOW_SECONDS
    sample_count: int = 0

    def __post_init__(self):
        if self.state not in HEALTH_STATES:
            raise ValueError(f"invalid_health_state:{self.state}")
        object.__setattr__(self, "provider_id", str(self.provider_id or ""))
        object.__setattr__(self, "model_id", str(self.model_id or ""))
        object.__setattr__(self, "recent_failure_count", int(self.recent_failure_count))
        object.__setattr__(self, "recent_success_count", int(self.recent_success_count))
        object.__setattr__(self, "sample_count", int(self.sample_count))
        object.__setattr__(self, "window_seconds", int(self.window_seconds))
        until = self.cooldown_until
        if until is not None and until.tzinfo is None:
            object.__setattr__(
                self, "cooldown_until", until.replace(tzinfo=timezone.utc)
            )

    @property
    def auto_eligible(self) -> bool:
        """Unknown and degraded remain eligible; only active cooldown excludes."""

        return self.state != HEALTH_COOLDOWN


@dataclass(frozen=True)
class _HealthEvent:
    kind: str  # success | failure
    timestamp: datetime
    error_class: str = ""


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


def load_routing_health_policy(*, env: dict | None = None) -> RoutingHealthPolicy:
    source = env if env is not None else os.environ
    return RoutingHealthPolicy(
        window_seconds=_parse_int(
            source.get("ROUTING_HEALTH_WINDOW_SECONDS"),
            DEFAULT_HEALTH_WINDOW_SECONDS,
        ),
        failure_threshold=_parse_int(
            source.get("ROUTING_HEALTH_FAILURE_THRESHOLD"),
            DEFAULT_HEALTH_FAILURE_THRESHOLD,
        ),
        cooldown_seconds=_parse_int(
            source.get("ROUTING_HEALTH_COOLDOWN_SECONDS"),
            DEFAULT_HEALTH_COOLDOWN_SECONDS,
        ),
        enabled=_parse_bool(
            source.get("ROUTING_HEALTH_ENABLED"),
            DEFAULT_HEALTH_ENABLED,
        ),
    )


def is_qualifying_provider_failure(exc: BaseException | None, *, error_code: str = "") -> bool:
    """True only for operational provider/model failures (not budget/capability/HITL)."""

    if exc is None and not error_code:
        return False
    name = type(exc).__name__ if exc is not None else ""
    blob = f"{name} {error_code}".lower()
    for marker in _IGNORED_FAILURE_MARKERS:
        if marker.lower() in blob:
            return False
    for marker in _QUALIFYING_FAILURE_MARKERS:
        if marker.lower() in blob:
            return True
    # Explicit provider error modules often end with ProviderError.
    if name.endswith("ProviderError"):
        return True
    if error_code:
        code = error_code.lower()
        if code.startswith("provider_") or code in {
            "timeout",
            "provider_timeout",
            "provider_5xx",
            "connection_error",
        }:
            return True
    return False


class ProviderHealthTracker:
    """Process-local rolling health tracker used by ModelRouter / ExpertManager.

    Implements ``ProviderHealthStore``. Instances do not share mutable state;
    constructing two trackers (e.g. two workers) yields independent cooldowns.
    """

    STATE_SCOPE = STATE_SCOPE_PROCESS_LOCAL

    def __init__(self, policy: RoutingHealthPolicy | None = None):
        self.policy = policy or RoutingHealthPolicy()
        self._events: dict[tuple[str, str], Deque[_HealthEvent]] = {}
        self._cooldown_until: dict[tuple[str, str], datetime] = {}
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
        events = self._events.get(key)
        if not events:
            return
        while events and (now - events[0].timestamp) > window:
            events.popleft()

    def record_failure(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        error_class: str = "",
        now: datetime | None = None,
    ) -> ProviderHealthSnapshot:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        key = self._key(provider_id, model_id)
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            bucket.append(
                _HealthEvent(kind="failure", timestamp=stamp, error_class=error_class)
            )
            self._prune(key, stamp)
            failures = sum(1 for e in bucket if e.kind == "failure")
            if failures >= self.policy.failure_threshold:
                self._cooldown_until[key] = stamp + timedelta(
                    seconds=self.policy.cooldown_seconds
                )
        return self.snapshot(provider_id, model_id, now=stamp)

    def record_success(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now: datetime | None = None,
    ) -> ProviderHealthSnapshot:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        key = self._key(provider_id, model_id)
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            bucket.append(_HealthEvent(kind="success", timestamp=stamp))
            self._prune(key, stamp)
            # Successful execution ends cooldown and clears failure streak window.
            self._cooldown_until.pop(key, None)
            # Keep only recent successes after recovery to avoid sticky degraded.
            kept = deque(e for e in bucket if e.kind == "success")
            self._events[key] = kept
        return self.snapshot(provider_id, model_id, now=stamp)

    def snapshot(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now: datetime | None = None,
    ) -> ProviderHealthSnapshot:
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        key = self._key(provider_id, model_id)
        with self._lock:
            self._prune(key, stamp)
            until = self._cooldown_until.get(key)
            if until is not None and until <= stamp:
                self._cooldown_until.pop(key, None)
                until = None
            events = tuple(self._events.get(key, ()))
            failures = sum(1 for e in events if e.kind == "failure")
            successes = sum(1 for e in events if e.kind == "success")
            samples = len(events)

        if until is not None and until > stamp:
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_COOLDOWN,
                recent_failure_count=failures,
                recent_success_count=successes,
                cooldown_until=until,
                reason_code=REASON_COOLDOWN_ACTIVE,
                window_seconds=self.policy.window_seconds,
                sample_count=samples,
            )
        if samples == 0:
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_UNKNOWN,
                reason_code=REASON_INSUFFICIENT_SAMPLES,
                window_seconds=self.policy.window_seconds,
                sample_count=0,
            )
        if failures > 0:
            return ProviderHealthSnapshot(
                provider_id=provider_id,
                model_id=model_id,
                state=HEALTH_DEGRADED,
                recent_failure_count=failures,
                recent_success_count=successes,
                reason_code=REASON_REPEATED_FAILURES if failures > 1 else None,
                window_seconds=self.policy.window_seconds,
                sample_count=samples,
            )
        return ProviderHealthSnapshot(
            provider_id=provider_id,
            model_id=model_id,
            state=HEALTH_HEALTHY,
            recent_failure_count=0,
            recent_success_count=successes,
            window_seconds=self.policy.window_seconds,
            sample_count=samples,
        )

    def is_auto_eligible(
        self,
        provider_id: str,
        model_id: str = "",
        *,
        now: datetime | None = None,
    ) -> bool:
        if not self.policy.enabled:
            return True
        return self.snapshot(provider_id, model_id, now=now).auto_eligible
