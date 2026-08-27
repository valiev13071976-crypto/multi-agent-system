"""In-process rate limiting — traffic control, not FinOps."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from security.config import (
    rate_limit_per_tenant_per_minute,
    rate_limit_per_user_per_minute,
    rate_limit_unauthenticated_per_minute,
)
from security.errors import RateLimitedError


@dataclass
class _Bucket:
    window_start: float
    count: int = 0


class RateLimiter:
    """Sliding-window counter per key. Process-local."""

    def __init__(
        self,
        *,
        user_limit: int | None = None,
        tenant_limit: int | None = None,
        ip_limit: int | None = None,
        window_seconds: float = 60.0,
    ):
        self.user_limit = user_limit or rate_limit_per_user_per_minute()
        self.tenant_limit = tenant_limit or rate_limit_per_tenant_per_minute()
        self.ip_limit = ip_limit or rate_limit_unauthenticated_per_minute()
        self.window_seconds = window_seconds
        self._buckets: dict[str, _Bucket] = defaultdict(lambda: _Bucket(window_start=time.monotonic()))

    def _check(self, key: str, limit: int) -> None:
        now = time.monotonic()
        bucket = self._buckets[key]
        if now - bucket.window_start >= self.window_seconds:
            bucket.window_start = now
            bucket.count = 0
        bucket.count += 1
        if bucket.count > limit:
            retry = max(0.0, self.window_seconds - (now - bucket.window_start))
            raise RateLimitedError(retry_after_seconds=retry)

    def check_authenticated(
        self, *, tenant_id: str, user_id: str
    ) -> None:
        self._check(f"user:{tenant_id}:{user_id}", self.user_limit)
        self._check(f"tenant:{tenant_id}", self.tenant_limit)

    def check_unauthenticated(self, *, source_ip: str) -> None:
        ip = source_ip or "unknown"
        self._check(f"ip:{ip}", self.ip_limit)
