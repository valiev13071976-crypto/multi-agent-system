"""FH.5–FH.10 — ProviderGovernor concurrency, rates, 429, breaker, admit/backpressure."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from providers.errors import is_rate_limit_error
from providers.governor import (
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    GovernorLimits,
    InMemoryProviderGovernorStore,
    ProviderCapacityUnavailable,
    ProviderGovernor,
)
from workflow.run_envelope import RunEnvelope


T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


class _RateLimitExc(Exception):
    status_code = 429
    headers = {"Retry-After": "2"}


class FHProviderGovernorLimitsTests(unittest.TestCase):
    def test_concurrency_saturation_and_release(self):
        lim = GovernorLimits(max_concurrency=1, max_rpm=None, max_qps=None, enabled=True)
        gov = ProviderGovernor(InMemoryProviderGovernorStore(lim))
        s1 = gov.acquire(provider_id="p", model_id="m", lane="interactive")
        with self.assertRaises(ProviderCapacityUnavailable) as ctx:
            gov.acquire(provider_id="p", model_id="m", lane="interactive")
        self.assertEqual(ctx.exception.reason, "provider_concurrency_limit")
        gov.release(s1)
        s2 = gov.acquire(provider_id="p", model_id="m", lane="interactive")
        gov.release(s2)

    def test_qps_limit(self):
        lim = GovernorLimits(
            max_concurrency=10, max_rpm=None, max_qps=1, max_tpm=None, enabled=True
        )
        store = InMemoryProviderGovernorStore(lim)
        s1 = store.acquire(provider_id="p", model_id="m", now=T0)
        with self.assertRaises(ProviderCapacityUnavailable) as ctx:
            store.acquire(provider_id="p", model_id="m", now=T0)
        self.assertEqual(ctx.exception.reason, "provider_qps_limit")
        store.release(s1)
        # Next second window allows again.
        store.acquire(provider_id="p", model_id="m", now=T0 + timedelta(seconds=1))

    def test_rpm_limit(self):
        lim = GovernorLimits(
            max_concurrency=100, max_rpm=1, max_qps=None, enabled=True
        )
        store = InMemoryProviderGovernorStore(lim)
        store.acquire(provider_id="p", model_id="m", now=T0)
        with self.assertRaises(ProviderCapacityUnavailable) as ctx:
            store.acquire(provider_id="p", model_id="m", now=T0 + timedelta(seconds=1))
        self.assertEqual(ctx.exception.reason, "provider_rpm_limit")

    def test_tpm_limit_and_unknown_tokens_noop(self):
        lim = GovernorLimits(
            max_concurrency=10, max_rpm=None, max_qps=None, max_tpm=10, enabled=True
        )
        store = InMemoryProviderGovernorStore(lim)
        store.acquire(provider_id="p", model_id="m", now=T0)
        store.record_tokens("p", "m", tokens=None)  # unknown → no-op
        store.record_tokens("p", "m", tokens=10)
        with self.assertRaises(ProviderCapacityUnavailable) as ctx:
            store.acquire(provider_id="p", model_id="m", now=T0 + timedelta(seconds=1))
        self.assertEqual(ctx.exception.reason, "provider_tpm_limit")

    def test_429_retry_after_throttle_not_permanent(self):
        lim = GovernorLimits(max_concurrency=5, max_rpm=None, max_qps=None, enabled=True)
        gov = ProviderGovernor(InMemoryProviderGovernorStore(lim))
        gov.record_429("p", "m", retry_after_seconds=2.0, now=T0)
        with self.assertRaises(ProviderCapacityUnavailable) as ctx:
            gov.acquire(provider_id="p", model_id="m", now=T0 + timedelta(seconds=1))
        self.assertEqual(ctx.exception.reason, "provider_429_throttle")
        # After throttle window — available again (not permanent death).
        slot = gov.acquire(provider_id="p", model_id="m", now=T0 + timedelta(seconds=3))
        gov.release(slot)
        self.assertTrue(is_rate_limit_error(_RateLimitExc()))

    def test_circuit_open_half_open_recovery(self):
        lim = GovernorLimits(
            max_concurrency=5,
            max_rpm=None,
            max_qps=None,
            failure_threshold=2,
            cooldown_seconds=5.0,
            half_open_probe_limit=1,
            enabled=True,
        )
        store = InMemoryProviderGovernorStore(lim)
        store.record_failure("p", "m", error_code="timeout", now=T0)
        store.record_failure("p", "m", error_code="timeout", now=T0)
        self.assertEqual(store.breaker_state("p", "m", now=T0), STATE_OPEN)
        with self.assertRaises(ProviderCapacityUnavailable):
            store.acquire(provider_id="p", model_id="m", now=T0 + timedelta(seconds=1))
        # Cooldown → HALF_OPEN probe.
        slot = store.acquire(provider_id="p", model_id="m", now=T0 + timedelta(seconds=6))
        self.assertEqual(store.breaker_state("p", "m", now=T0 + timedelta(seconds=6)), STATE_HALF_OPEN)
        store.release(slot)
        store.record_success("p", "m")
        self.assertEqual(store.breaker_state("p", "m", now=T0 + timedelta(seconds=6)), STATE_CLOSED)

    def test_admit_deadline_expired(self):
        lim = GovernorLimits(enabled=True, max_rpm=None, max_qps=None)
        gov = ProviderGovernor(InMemoryProviderGovernorStore(lim))
        envelope = RunEnvelope.create(
            workflow_id="wf",
            task_id="t",
            tenant_id="tenant-a",
            request_id="r",
            correlation_id="c",
            trace_id="tr",
            deadline_at=T0,
        )
        with self.assertRaises(ProviderCapacityUnavailable) as ctx:
            gov.admit(
                provider_id="p",
                model_id="m",
                envelope=envelope,
                now=T0 + timedelta(seconds=1),
            )
        self.assertEqual(ctx.exception.reason, "provider_deadline_expired")

    def test_provider_isolation_breaker(self):
        lim = GovernorLimits(
            max_concurrency=5,
            max_rpm=None,
            max_qps=None,
            failure_threshold=1,
            enabled=True,
        )
        store = InMemoryProviderGovernorStore(lim)
        store.record_failure("openai", "m", error_code="boom")
        self.assertEqual(store.breaker_state("openai", "m"), STATE_OPEN)
        slot = store.acquire(provider_id="anthropic", model_id="m", now=T0)
        store.release(slot)


if __name__ == "__main__":
    unittest.main()
