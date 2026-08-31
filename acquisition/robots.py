"""Centralized robots.txt policy — fetch/cache/parse with fail-closed semantics."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser


class RobotsUnavailableError(Exception):
    """Robots rules could not be resolved for the host."""


@dataclass
class _RobotsEntry:
    loaded_at: float
    parser: RobotFileParser


class RobotsPolicy:
    """Tenant-scoped robots.txt cache with deterministic allow/deny checks."""

    def __init__(
        self,
        *,
        user_agent: str = "PandaAcquisitionBot",
        ttl_seconds: float = 3600.0,
        fail_closed: bool = True,
    ):
        self.user_agent = str(user_agent or "PandaAcquisitionBot")
        self.ttl_seconds = float(ttl_seconds)
        self.fail_closed = bool(fail_closed)
        self._cache: dict[tuple[str, str], _RobotsEntry] = {}

    def load(self, tenant_id: str, host: str, robots_text: str) -> None:
        """Load robots.txt body for a host (tests or prefetch)."""
        h = (host or "").lower().rstrip(".")
        tid = str(tenant_id or "").strip() or "default"
        parser = RobotFileParser()
        parser.set_url(f"https://{h}/robots.txt")
        parser.parse(str(robots_text or "").splitlines())
        self._cache[(tid, h)] = _RobotsEntry(time.time(), parser)

    def clear(self) -> None:
        self._cache.clear()

    def _entry(self, tenant_id: str, host: str) -> _RobotsEntry | None:
        key = (str(tenant_id or "").strip() or "default", (host or "").lower().rstrip("."))
        entry = self._cache.get(key)
        if entry is None:
            return None
        if self.ttl_seconds > 0 and (time.time() - entry.loaded_at) > self.ttl_seconds:
            self._cache.pop(key, None)
            return None
        return entry

    def is_allowed(self, url: str, *, tenant_id: str = "default") -> bool:
        """Return whether URL is allowed by cached robots rules."""
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        if not host:
            if self.fail_closed:
                raise RobotsUnavailableError("robots_host_missing")
            return True
        entry = self._entry(tenant_id, host)
        if entry is None:
            if self.fail_closed:
                raise RobotsUnavailableError("robots_cache_miss")
            return True
        return bool(entry.parser.can_fetch(self.user_agent, url))

    def checker(self, tenant_id: str) -> Callable[[str], bool]:
        """Build a tenant-bound callback for ``evaluate_url`` / ``SourcePolicy``."""

        def _check(url: str) -> bool:
            return self.is_allowed(url, tenant_id=tenant_id)

        return _check


def make_robots_checker(policy: RobotsPolicy, tenant_id: str) -> Callable[[str], bool]:
    return policy.checker(tenant_id)
