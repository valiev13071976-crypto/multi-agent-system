"""Source policy evaluator — trusted host/path/robots contract.

Payloads cannot override trusted host allowlists or tenant restrictions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from acquisition.models import CrawlPolicy, SourceDefinition, SourceDescriptor
from tools.url_safety import UnsafeUrlError, validate_http_url


class PolicyVerdict(str, Enum):
    PERMITTED = "permitted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PolicyDecision:
    verdict: PolicyVerdict
    reason: str
    url: str = ""
    host: str = ""

    @property
    def permitted(self) -> bool:
        return self.verdict == PolicyVerdict.PERMITTED


def _hosts_from_source(source: SourceDefinition | SourceDescriptor) -> tuple[str, ...]:
    if isinstance(source, SourceDefinition):
        return tuple(h.lower() for h in source.allowed_hosts)
    return tuple(d.lower() for d in (source.allowed_domains or ()))


def _path_lists(source: SourceDefinition | SourceDescriptor, policy: CrawlPolicy | None):
    allow = list(getattr(source, "path_allow", ()) or ())
    deny = list(getattr(source, "path_deny", ()) or ())
    if policy is not None:
        allow = list(policy.path_allow or allow)
        deny = list(policy.path_deny or deny)
    if isinstance(source, SourceDescriptor):
        meta = dict(source.metadata or {})
        allow = list(meta.get("path_allow") or allow)
        deny = list(meta.get("path_deny") or deny)
    return tuple(allow), tuple(deny)


def host_matches(host: str, allowed: tuple[str, ...]) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h or not allowed:
        return False
    for domain in allowed:
        d = domain.lower().rstrip(".")
        if h == d or h.endswith("." + d):
            return True
    return False


def path_allowed(path: str, *, allow: tuple[str, ...], deny: tuple[str, ...]) -> bool:
    p = path or "/"
    for prefix in deny:
        if prefix and p.startswith(prefix):
            return False
    if not allow:
        return True
    return any(p.startswith(prefix) for prefix in allow if prefix)


def evaluate_url(
    url: str,
    *,
    source: SourceDefinition | SourceDescriptor,
    policy: CrawlPolicy | None = None,
    robots_allowed=None,
    payload_host_override: str | None = None,
) -> PolicyDecision:
    """Evaluate URL against trusted source policy.

    ``payload_host_override`` is intentionally ignored for allowlist decisions —
    untrusted callers cannot widen the host set.
    """
    _ = payload_host_override  # discard — never trust payload overrides
    raw = str(url or "").strip()
    if not raw:
        return PolicyDecision(PolicyVerdict.DENIED, "empty_url", url=raw)

    try:
        safe = validate_http_url(raw)
    except UnsafeUrlError as exc:
        return PolicyDecision(PolicyVerdict.DENIED, f"unsafe_url:{exc.reason}", url=raw)

    parsed = urlparse(safe)
    host = (parsed.hostname or "").lower()
    allowed = _hosts_from_source(source)
    if not allowed:
        return PolicyDecision(PolicyVerdict.UNKNOWN, "no_allowed_hosts", url=safe, host=host)
    if not host_matches(host, allowed):
        return PolicyDecision(PolicyVerdict.DENIED, "host_not_allowed", url=safe, host=host)

    crawl = policy
    if crawl is None and isinstance(source, SourceDefinition):
        crawl = source.crawl_policy
    allow, deny = _path_lists(source, crawl)
    if not path_allowed(parsed.path or "/", allow=allow, deny=deny):
        return PolicyDecision(PolicyVerdict.DENIED, "path_denied", url=safe, host=host)

    if robots_allowed is not None and (crawl is None or crawl.respect_robots):
        try:
            if not robots_allowed(safe):
                return PolicyDecision(PolicyVerdict.DENIED, "robots_denied", url=safe, host=host)
        except Exception:
            return PolicyDecision(PolicyVerdict.UNAVAILABLE, "robots_unavailable", url=safe, host=host)

    if not getattr(source, "enabled", True):
        return PolicyDecision(PolicyVerdict.UNAVAILABLE, "source_disabled", url=safe, host=host)

    return PolicyDecision(PolicyVerdict.PERMITTED, "ok", url=safe, host=host)


def merge_trusted_hosts(
    source: SourceDefinition | SourceDescriptor,
    *,
    payload_hosts: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    """Return trusted hosts only — payload hosts never expand the allowlist."""
    trusted = _hosts_from_source(source)
    _ = payload_hosts  # ignored
    return trusted


class SourcePolicy:
    """Thin facade used by planner/crawler."""

    def __init__(self, *, robots_allowed=None):
        self._robots_allowed = robots_allowed

    def evaluate(
        self,
        url: str,
        *,
        source: SourceDefinition | SourceDescriptor,
        policy: CrawlPolicy | None = None,
        payload_host_override: str | None = None,
    ) -> PolicyDecision:
        return evaluate_url(
            url,
            source=source,
            policy=policy,
            robots_allowed=self._robots_allowed,
            payload_host_override=payload_host_override,
        )
