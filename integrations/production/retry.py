"""Bounded retry helpers for production provider calls."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0
    deadline_seconds: float = 60.0


def is_retryable(category: ProviderErrorCategory) -> bool:
    return category in {
        ProviderErrorCategory.RATE_LIMITED,
        ProviderErrorCategory.TIMEOUT,
        ProviderErrorCategory.NETWORK_ERROR,
        ProviderErrorCategory.PROVIDER_UNAVAILABLE,
    }


def execute_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    on_retry: Callable[[int, ProductionProviderError], None] | None = None,
) -> T:
    pol = policy or RetryPolicy()
    started = time.monotonic()
    last_exc: Exception | None = None
    for attempt in range(1, pol.max_attempts + 1):
        if time.monotonic() - started > pol.deadline_seconds:
            break
        try:
            return fn()
        except ProductionProviderError as exc:
            last_exc = exc
            if not exc.retryable or attempt >= pol.max_attempts:
                raise
            if on_retry:
                on_retry(attempt, exc)
            delay = exc.retry_after_seconds
            if delay is None:
                delay = min(pol.base_delay_seconds * (2 ** (attempt - 1)), pol.max_delay_seconds)
            remaining = pol.deadline_seconds - (time.monotonic() - started)
            time.sleep(min(delay, max(0.0, remaining)))
        except Exception as exc:
            last_exc = exc
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("retry_exhausted")
