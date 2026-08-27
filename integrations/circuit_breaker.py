"""Reusable circuit breaker for external integrations."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from integrations.contracts import CircuitBreakerPolicy
from integrations.errors import CircuitOpenError

STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"

# Failures that must NOT open the breaker
NON_OPERATIONAL = frozenset(
    {
        "integration_access_denied",
        "scope_insufficient",
        "tool_argument_invalid",
        "tool_permission_denied",
        "hitl_denied",
        "capability_denied",
        "validation_error",
        "idempotency_conflict",
    }
)


@dataclass
class CircuitState:
    state: str = STATE_CLOSED
    failures: int = 0
    opened_at: float | None = None
    half_open_probes: int = 0
    window_start: float | None = None


class CircuitBreaker:
    def __init__(self, policy: CircuitBreakerPolicy | None = None):
        self.policy = policy or CircuitBreakerPolicy()
        self._states: dict[str, CircuitState] = {}
        self._lock = threading.RLock()

    def _key(self, tenant_id: str, integration_id: str) -> str:
        return f"{tenant_id}:{integration_id}"

    def get_state(self, tenant_id: str, integration_id: str) -> str:
        with self._lock:
            st = self._states.get(self._key(tenant_id, integration_id))
            if st is None:
                return STATE_CLOSED
            self._maybe_transition(st)
            return st.state

    def assert_allow(self, tenant_id: str, integration_id: str) -> None:
        with self._lock:
            key = self._key(tenant_id, integration_id)
            st = self._states.setdefault(key, CircuitState())
            self._maybe_transition(st)
            if st.state == STATE_OPEN:
                raise CircuitOpenError("circuit_open")
            if st.state == STATE_HALF_OPEN:
                if st.half_open_probes >= self.policy.half_open_probe_limit:
                    raise CircuitOpenError("circuit_open")
                st.half_open_probes += 1

    def record_success(self, tenant_id: str, integration_id: str) -> None:
        with self._lock:
            st = self._states.setdefault(self._key(tenant_id, integration_id), CircuitState())
            st.state = STATE_CLOSED
            st.failures = 0
            st.opened_at = None
            st.half_open_probes = 0
            st.window_start = None

    def record_failure(
        self, tenant_id: str, integration_id: str, *, error_code: str = ""
    ) -> None:
        if error_code in NON_OPERATIONAL:
            return
        with self._lock:
            st = self._states.setdefault(self._key(tenant_id, integration_id), CircuitState())
            now = time.monotonic()
            if st.window_start is None or (now - st.window_start) > self.policy.window_seconds:
                st.window_start = now
                st.failures = 0
            st.failures += 1
            if st.state == STATE_HALF_OPEN:
                st.state = STATE_OPEN
                st.opened_at = now
                st.half_open_probes = 0
                return
            if st.failures >= self.policy.failure_threshold:
                st.state = STATE_OPEN
                st.opened_at = now

    def _maybe_transition(self, st: CircuitState) -> None:
        if st.state != STATE_OPEN or st.opened_at is None:
            return
        if (time.monotonic() - st.opened_at) >= self.policy.cooldown_seconds:
            st.state = STATE_HALF_OPEN
            st.half_open_probes = 0
