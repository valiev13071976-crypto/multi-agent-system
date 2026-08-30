"""Controlled web crawler — durable frontier, policy, politeness, ToolGateway-only fetch."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from acquisition.errors import (
    AcquisitionDeniedError,
    AcquisitionTimeoutError,
    CapacityRejectedError,
    JobCancelledError,
    RateLimitedError,
)
from acquisition.manager import AcquisitionManager
from acquisition.models import (
    ACQ_HTTP_GET,
    FRONTIER_CLAIMED,
    FRONTIER_COMPLETED,
    FRONTIER_FAILED,
    FRONTIER_PENDING,
    FRONTIER_RETRY,
    FRONTIER_SKIPPED,
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_PARTIAL,
    JOB_RUNNING,
    RESOURCE_DENIED,
    RESOURCE_FAILED,
    RESOURCE_FETCHED,
    RESOURCE_SKIPPED,
    AcquisitionJob,
    AcquisitionRequest,
    AcquiredResource,
    CrawlCheckpoint,
    CrawlPolicy,
    FrontierEntry,
    RawArtifact,
    SourceDefinition,
    SourceDescriptor,
    new_id,
    utc_now,
)
from acquisition.source_policy import SourcePolicy, evaluate_url, host_matches
from tools.url_safety import UnsafeUrlError, validate_http_url

# Common tracking params stripped when policy.ignore_tracking_params is True.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
    }
)

FetchFn = Callable[[str], Awaitable[RawArtifact]]


def canonicalize_url(
    url: str,
    *,
    ignore_tracking_params: bool = True,
    keep_query: bool = True,
) -> str:
    """Deterministic URL canonicalization — policy-controlled tracking param ignore."""
    parsed = urlparse(str(url or "").strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = ""
    if keep_query and parsed.query:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if ignore_tracking_params:
            pairs = [(k, v) for k, v in pairs if k.lower() not in TRACKING_PARAMS]
        pairs.sort(key=lambda kv: (kv[0], kv[1]))
        query = urlencode(pairs, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


@dataclass
class CrawlLimits:
    max_depth: int = 1
    max_pages: int = 10
    allowed_content_types: tuple[str, ...] = (
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "text/plain",
        "text/csv",
        "application/csv",
    )
    respect_robots: bool = True
    max_frontier: int = 500
    max_redirects: int = 5
    per_host_concurrency: int = 2
    min_interval_seconds: float = 0.0
    ignore_tracking_params: bool = True
    path_allow: tuple[str, ...] = ()
    path_deny: tuple[str, ...] = ()
    deadline_seconds: float | None = None
    max_retries_per_url: int = 3

    def to_policy(self) -> CrawlPolicy:
        return CrawlPolicy(
            max_depth=self.max_depth,
            max_pages=self.max_pages,
            max_frontier=self.max_frontier,
            per_host_concurrency=self.per_host_concurrency,
            min_interval_seconds=self.min_interval_seconds,
            max_redirects=self.max_redirects,
            ignore_tracking_params=self.ignore_tracking_params,
            allowed_content_types=self.allowed_content_types,
            path_allow=self.path_allow,
            path_deny=self.path_deny,
            respect_robots=self.respect_robots,
            deadline_seconds=self.deadline_seconds,
            max_retries_per_url=self.max_retries_per_url,
        )


@dataclass
class CrawlResult:
    artifacts: tuple[RawArtifact, ...]
    visited: tuple[str, ...]
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    resources: tuple[AcquiredResource, ...] = ()
    checkpoint: CrawlCheckpoint | None = None
    status: str = JOB_COMPLETED
    pages_fetched: int = 0
    pages_failed: int = 0
    pages_skipped: int = 0


@dataclass
class _HostPoliteness:
    last_fetch_at: float = 0.0
    inflight: int = 0
    backoff_until: float = 0.0


class ControlledCrawler:
    """Bounded crawler — seed URLs, domain allowlist, depth/page limits, durable frontier."""

    def __init__(
        self,
        manager: AcquisitionManager,
        *,
        robots_allowed=None,
        store=None,
        fetch_fn: FetchFn | None = None,
        source_policy: SourcePolicy | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.manager = manager
        self._robots_allowed = robots_allowed
        self.store = store
        self._fetch_fn = fetch_fn
        self.policy_engine = source_policy or SourcePolicy(robots_allowed=robots_allowed)
        self._clock = clock or time.monotonic
        self._host_state: dict[str, _HostPoliteness] = {}
        self._cancel_jobs: set[str] = set()
        try:
            from acquisition.observability import get_observer

            self.observer = get_observer()
        except Exception:
            self.observer = None

    def request_cancel(self, job_id: str) -> None:
        self._cancel_jobs.add(str(job_id))

    def clear_cancel(self, job_id: str) -> None:
        self._cancel_jobs.discard(str(job_id))

    def _is_cancelled(self, job_id: str, job: AcquisitionJob | None = None) -> bool:
        if job_id in self._cancel_jobs:
            return True
        if job is not None and job.cancel_requested:
            return True
        if self.store is not None and hasattr(self.store, "get_job") and job is not None:
            latest = self.store.get_job(job_id, tenant_id=job.tenant_id)
            if latest is not None and (latest.cancel_requested or latest.status == JOB_CANCELLED):
                return True
        return False

    def _domain_ok(self, url: str, source: SourceDescriptor | SourceDefinition) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if isinstance(source, SourceDefinition):
            return host_matches(host, source.allowed_hosts)
        if not source.allowed_domains:
            return False
        return host_matches(host, tuple(source.allowed_domains))

    def _content_ok(self, artifact: RawArtifact, policy: CrawlPolicy) -> bool:
        ct = (artifact.content_type or "").split(";")[0].strip().lower()
        if not ct:
            return True
        return any(ct.startswith(a) for a in policy.allowed_content_types)

    def _extract_links(self, artifact: RawArtifact, base: str, policy: CrawlPolicy) -> list[str]:
        import re

        text = artifact.content_text or ""
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', text, flags=re.I)
        out = []
        for href in hrefs:
            if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
                continue
            absolute = canonicalize_url(
                urljoin(base, href),
                ignore_tracking_params=policy.ignore_tracking_params,
            )
            out.append(absolute)
        return out

    async def _wait_polite(self, host: str, policy: CrawlPolicy) -> None:
        state = self._host_state.setdefault(host, _HostPoliteness())
        now = self._clock()
        if state.backoff_until > now:
            await asyncio.sleep(max(0.0, state.backoff_until - now))
        while state.inflight >= max(1, int(policy.per_host_concurrency)):
            await asyncio.sleep(0.01)
            now = self._clock()
            if state.backoff_until > now:
                await asyncio.sleep(max(0.0, state.backoff_until - now))
        elapsed = self._clock() - state.last_fetch_at
        gap = float(policy.min_interval_seconds or 0.0)
        if gap > 0 and elapsed < gap:
            await asyncio.sleep(gap - elapsed)
        state.inflight += 1

    def _release_host(self, host: str, *, retry_after: float | None = None) -> None:
        state = self._host_state.setdefault(host, _HostPoliteness())
        state.inflight = max(0, state.inflight - 1)
        state.last_fetch_at = self._clock()
        if retry_after is not None and retry_after > 0:
            state.backoff_until = max(state.backoff_until, self._clock() + float(retry_after))

    async def _fetch(
        self,
        url: str,
        *,
        source: SourceDescriptor,
        tenant_id: str,
        workflow_id: str,
    ) -> RawArtifact:
        if self._fetch_fn is not None:
            return await self._fetch_fn(url)
        req = AcquisitionRequest(
            source_id=source.source_id,
            target=url,
            acquisition_type=ACQ_HTTP_GET,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
        )
        return await self.manager.acquire(req)

    def _persist_frontier(self, entry: FrontierEntry) -> None:
        if self.store is not None and hasattr(self.store, "save_frontier_entry"):
            self.store.save_frontier_entry(entry)

    def _persist_resource(self, resource: AcquiredResource) -> AcquiredResource:
        if self.store is not None and hasattr(self.store, "save_resource"):
            return self.store.save_resource(resource)
        return resource

    def _persist_checkpoint(self, checkpoint: CrawlCheckpoint) -> None:
        if self.store is not None and hasattr(self.store, "save_checkpoint"):
            self.store.save_checkpoint(checkpoint)

    def _load_frontier(self, job_id: str, tenant_id: str) -> list[FrontierEntry]:
        if self.store is not None and hasattr(self.store, "list_frontier"):
            return list(
                self.store.list_frontier(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    statuses=(FRONTIER_PENDING, FRONTIER_RETRY),
                )
            )
        return []

    def _frontier_lookup(
        self, *, job_id: str, tenant_id: str, canonical_url: str
    ) -> FrontierEntry | None:
        if self.store is not None and hasattr(self.store, "list_frontier"):
            for entry in self.store.list_frontier(job_id=job_id, tenant_id=tenant_id):
                if entry.canonical_url == canonical_url:
                    return entry
        return None

    def _ensure_frontier_entry(
        self,
        *,
        job_id: str,
        tenant_id: str,
        url: str,
        canonical_url: str,
        depth: int,
        parent: str,
        retry_count: int,
        status: str,
        error_code: str = "",
        cache: dict[str, FrontierEntry],
    ) -> FrontierEntry:
        existing = cache.get(canonical_url) or self._frontier_lookup(
            job_id=job_id, tenant_id=tenant_id, canonical_url=canonical_url
        )
        if existing is not None:
            entry = replace(
                existing,
                url=url or existing.url,
                depth=depth,
                parent_url=parent,
                retry_count=int(retry_count),
                status=status,
                error_code=error_code,
                updated_at=utc_now(),
            )
        else:
            entry = FrontierEntry(
                entry_id=new_id("fr-"),
                job_id=job_id,
                tenant_id=tenant_id,
                url=url,
                canonical_url=canonical_url,
                status=status,
                depth=depth,
                parent_url=parent,
                retry_count=int(retry_count),
                error_code=error_code,
            )
        cache[canonical_url] = entry
        self._persist_frontier(entry)
        return entry

    @staticmethod
    def _is_retryable_fetch_error(exc: BaseException) -> bool:
        if isinstance(exc, (RateLimitedError, AcquisitionTimeoutError)):
            return True
        code = str(getattr(exc, "error_code", "") or "").lower()
        return any(tok in code for tok in ("rate_limit", "timeout", "transient", "unavailable"))

    @staticmethod
    def _terminal_reason_for(exc: BaseException) -> str:
        if isinstance(exc, RateLimitedError):
            return "rate_limited"
        if isinstance(exc, AcquisitionTimeoutError):
            return "fetch_timeout"
        code = str(getattr(exc, "error_code", "") or "").strip()
        return code or "fetch_failed"

    def _schedule_or_exhaust_retry(
        self,
        *,
        job_id: str,
        tenant_id: str,
        source_id: str,
        url: str,
        depth: int,
        parent: str,
        retry_count: int,
        policy: CrawlPolicy,
        exc: BaseException,
        memory_queue: list[tuple[str, int, str, int]],
        frontier_cache: dict[str, FrontierEntry],
        resources: list[AcquiredResource],
        errors: list[str],
        observer=None,
    ) -> tuple[bool, int]:
        """Return (requeued, pages_failed_delta).

        Semantics: ``retry_count`` = retries already performed before this failed attempt.
        After failure, if ``retry_count < max_retries_per_url``, schedule another retry
        with ``retry_count + 1``. Max fetch attempts = 1 + max_retries_per_url.
        """
        reason = self._terminal_reason_for(exc)
        max_retries = int(policy.max_retries_per_url)
        if retry_count < max_retries:
            new_count = int(retry_count) + 1
            self._ensure_frontier_entry(
                job_id=job_id,
                tenant_id=tenant_id,
                url=url,
                canonical_url=url,
                depth=depth,
                parent=parent,
                retry_count=new_count,
                status=FRONTIER_RETRY,
                error_code=reason,
                cache=frontier_cache,
            )
            # Requeue as pending/claimable with durable retry_count.
            self._ensure_frontier_entry(
                job_id=job_id,
                tenant_id=tenant_id,
                url=url,
                canonical_url=url,
                depth=depth,
                parent=parent,
                retry_count=new_count,
                status=FRONTIER_PENDING,
                error_code=reason,
                cache=frontier_cache,
            )
            memory_queue.append((url, depth, parent, new_count))
            errors.append(f"{url}:retry_scheduled:{reason}:{new_count}")
            if observer is not None and hasattr(observer, "on_retry_scheduled"):
                observer.on_retry_scheduled(reason=reason, retry_count=new_count)
            return True, 0

        self._ensure_frontier_entry(
            job_id=job_id,
            tenant_id=tenant_id,
            url=url,
            canonical_url=url,
            depth=depth,
            parent=parent,
            retry_count=int(retry_count),
            status=FRONTIER_FAILED,
            error_code=reason,
            cache=frontier_cache,
        )
        resources.append(
            self._persist_resource(
                AcquiredResource(
                    resource_id=new_id("res-"),
                    job_id=job_id,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    url=url,
                    canonical_url=url,
                    status=RESOURCE_FAILED,
                    depth=depth,
                    parent_url=parent,
                    provenance={
                        "error": reason,
                        "retry_count": int(retry_count),
                        "retry_exhausted": True,
                        "fetch_attempts": int(retry_count) + 1,
                    },
                )
            )
        )
        errors.append(f"{url}:{reason}:retry_exhausted")
        if observer is not None and hasattr(observer, "on_retry_exhausted"):
            observer.on_retry_exhausted(reason=reason, retry_count=int(retry_count))
        return False, 1

    async def crawl(
        self,
        *,
        source: SourceDescriptor | SourceDefinition,
        seeds: tuple[str, ...],
        tenant_id: str,
        workflow_id: str = "",
        limits: CrawlLimits | None = None,
        job: AcquisitionJob | None = None,
        resume: bool = False,
    ) -> CrawlResult:
        policy = (limits or CrawlLimits()).to_policy()
        if isinstance(source, SourceDefinition):
            descriptor = source.to_descriptor()
            if limits is None:
                policy = source.crawl_policy
        else:
            descriptor = source
        if not descriptor.allowed_domains:
            raise AcquisitionDeniedError("crawler_requires_allowed_domains")

        job_id = job.job_id if job else new_id("job-")
        job_obj = job
        deadline_at = None
        if policy.deadline_seconds is not None:
            deadline_at = self._clock() + float(policy.deadline_seconds)

        # Memory frontier when store lacks durable support (unit tests / legacy path).
        # Queue item: (canonical_url, depth, parent_url, retry_count)
        # retry_count = retries already performed; max fetch attempts = 1 + max_retries_per_url
        memory_queue: list[tuple[str, int, str, int]] = []
        visited: set[str] = set()
        skipped: list[str] = []
        errors: list[str] = []
        artifacts: list[RawArtifact] = []
        resources: list[AcquiredResource] = []
        pages_failed = 0
        pages_skipped = 0
        frontier_cache: dict[str, FrontierEntry] = {}
        observer = getattr(self, "observer", None)

        if resume and self.store is not None:
            for entry in self._load_frontier(job_id, tenant_id):
                frontier_cache[entry.canonical_url] = entry
                memory_queue.append(
                    (
                        entry.canonical_url,
                        entry.depth,
                        entry.parent_url,
                        int(entry.retry_count),
                    )
                )
            if hasattr(self.store, "list_resources"):
                for res in self.store.list_resources(job_id=job_id, tenant_id=tenant_id):
                    if res.canonical_url and res.status == RESOURCE_FETCHED:
                        visited.add(res.canonical_url)
                    if res.status == RESOURCE_FETCHED and res.raw_artifact_ref:
                        art = self.store.get_artifact(res.raw_artifact_ref, tenant_id=tenant_id)
                        if art is not None:
                            artifacts.append(art)
                            resources.append(res)
                    elif res.status == RESOURCE_FAILED and res.canonical_url:
                        visited.add(res.canonical_url)

        queued_urls = {item[0] for item in memory_queue}
        for seed in seeds:
            try:
                validate_http_url(seed)
            except UnsafeUrlError as exc:
                raise AcquisitionDeniedError("unsafe_seed_url") from exc
            canon = canonicalize_url(seed, ignore_tracking_params=policy.ignore_tracking_params)
            decision = evaluate_url(
                canon,
                source=source if isinstance(source, SourceDefinition) else descriptor,
                policy=policy,
                robots_allowed=self._robots_allowed,
            )
            if not decision.permitted:
                raise AcquisitionDeniedError(f"seed_denied:{decision.reason}")
            if canon not in visited and canon not in queued_urls:
                memory_queue.append((canon, 0, "", 0))
                queued_urls.add(canon)
                self._ensure_frontier_entry(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    url=seed,
                    canonical_url=canon,
                    depth=0,
                    parent="",
                    retry_count=0,
                    status=FRONTIER_PENDING,
                    cache=frontier_cache,
                )

        status = JOB_COMPLETED
        while memory_queue and len(artifacts) < policy.max_pages:
            if deadline_at is not None and self._clock() >= deadline_at:
                status = JOB_PARTIAL
                errors.append("deadline_exceeded")
                break
            if self._is_cancelled(job_id, job_obj):
                status = JOB_CANCELLED
                break

            url, depth, parent, retry_count = memory_queue.pop(0)
            if url in visited:
                pages_skipped += 1
                skipped.append(url)
                continue
            visited.add(url)

            decision = evaluate_url(
                url,
                source=source if isinstance(source, SourceDefinition) else descriptor,
                policy=policy,
                robots_allowed=self._robots_allowed,
            )
            if not decision.permitted:
                pages_skipped += 1
                skipped.append(url)
                self._ensure_frontier_entry(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    url=url,
                    canonical_url=url,
                    depth=depth,
                    parent=parent,
                    retry_count=retry_count,
                    status=FRONTIER_SKIPPED,
                    error_code=decision.reason,
                    cache=frontier_cache,
                )
                resources.append(
                    self._persist_resource(
                        AcquiredResource(
                            resource_id=new_id("res-"),
                            job_id=job_id,
                            tenant_id=tenant_id,
                            source_id=descriptor.source_id,
                            url=url,
                            canonical_url=url,
                            status=RESOURCE_DENIED,
                            depth=depth,
                            parent_url=parent,
                            provenance={"reason": decision.reason},
                        )
                    )
                )
                continue

            host = (urlparse(url).hostname or "").lower()
            self._ensure_frontier_entry(
                job_id=job_id,
                tenant_id=tenant_id,
                url=url,
                canonical_url=url,
                depth=depth,
                parent=parent,
                retry_count=retry_count,
                status=FRONTIER_CLAIMED,
                cache=frontier_cache,
            )
            await self._wait_polite(host, policy)
            art = None
            try:
                art = await self._fetch(
                    url,
                    source=descriptor,
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                )
            except Exception as exc:
                retry_after = None
                if isinstance(exc, RateLimitedError):
                    if exc.retry_after is not None:
                        retry_after = float(exc.retry_after)
                    else:
                        retry_after = min(60.0, 2.0 ** min(5, int(retry_count) + 1))
                self._release_host(host, retry_after=retry_after)
                if self._is_retryable_fetch_error(exc):
                    requeued, failed_delta = self._schedule_or_exhaust_retry(
                        job_id=job_id,
                        tenant_id=tenant_id,
                        source_id=descriptor.source_id,
                        url=url,
                        depth=depth,
                        parent=parent,
                        retry_count=retry_count,
                        policy=policy,
                        exc=exc,
                        memory_queue=memory_queue,
                        frontier_cache=frontier_cache,
                        resources=resources,
                        errors=errors,
                        observer=observer,
                    )
                    pages_failed += failed_delta
                    if requeued:
                        visited.discard(url)
                    continue
                pages_failed += 1
                code = self._terminal_reason_for(exc)
                errors.append(f"{url}:{code}")
                self._ensure_frontier_entry(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    url=url,
                    canonical_url=url,
                    depth=depth,
                    parent=parent,
                    retry_count=retry_count,
                    status=FRONTIER_FAILED,
                    error_code=code,
                    cache=frontier_cache,
                )
                resources.append(
                    self._persist_resource(
                        AcquiredResource(
                            resource_id=new_id("res-"),
                            job_id=job_id,
                            tenant_id=tenant_id,
                            source_id=descriptor.source_id,
                            url=url,
                            canonical_url=url,
                            status=RESOURCE_FAILED,
                            depth=depth,
                            parent_url=parent,
                            provenance={"error": code},
                        )
                    )
                )
                continue
            else:
                self._release_host(host)

            if not self._content_ok(art, policy):
                pages_skipped += 1
                skipped.append(url)
                self._ensure_frontier_entry(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    url=url,
                    canonical_url=url,
                    depth=depth,
                    parent=parent,
                    retry_count=retry_count,
                    status=FRONTIER_SKIPPED,
                    error_code="unsupported_content_type",
                    cache=frontier_cache,
                )
                resources.append(
                    self._persist_resource(
                        AcquiredResource(
                            resource_id=new_id("res-"),
                            job_id=job_id,
                            tenant_id=tenant_id,
                            source_id=descriptor.source_id,
                            url=url,
                            canonical_url=url,
                            status=RESOURCE_SKIPPED,
                            content_type=art.content_type,
                            content_hash=art.checksum,
                            raw_artifact_ref=art.artifact_id,
                            depth=depth,
                            parent_url=parent,
                            provenance={"reason": "content_type_filtered"},
                        )
                    )
                )
                continue

            self._ensure_frontier_entry(
                job_id=job_id,
                tenant_id=tenant_id,
                url=url,
                canonical_url=url,
                depth=depth,
                parent=parent,
                retry_count=retry_count,
                status=FRONTIER_COMPLETED,
                cache=frontier_cache,
            )
            artifacts.append(art)
            resources.append(
                self._persist_resource(
                    AcquiredResource(
                        resource_id=new_id("res-"),
                        job_id=job_id,
                        tenant_id=tenant_id,
                        source_id=descriptor.source_id,
                        url=url,
                        canonical_url=url,
                        status=RESOURCE_FETCHED,
                        content_type=art.content_type,
                        content_length=int(art.content_bytes_len or 0),
                        content_hash=art.checksum,
                        raw_artifact_ref=art.artifact_id,
                        depth=depth,
                        parent_url=parent,
                        provenance={
                            "job_id": job_id,
                            "stage": "raw",
                            "tenant_id": tenant_id,
                            "source_id": descriptor.source_id,
                            "retry_count": int(retry_count),
                            "fetch_attempts": int(retry_count) + 1,
                        },
                    )
                )
            )

            if depth < policy.max_depth:
                for link in self._extract_links(art, url, policy):
                    if link in visited or link in queued_urls:
                        continue
                    if not self._domain_ok(link, source if isinstance(source, SourceDefinition) else descriptor):
                        skipped.append(link)
                        continue
                    try:
                        validate_http_url(link)
                    except UnsafeUrlError:
                        skipped.append(link)
                        continue
                    link_decision = evaluate_url(
                        link,
                        source=source if isinstance(source, SourceDefinition) else descriptor,
                        policy=policy,
                        robots_allowed=self._robots_allowed,
                    )
                    if not link_decision.permitted:
                        skipped.append(link)
                        continue
                    if len(visited) + len(memory_queue) >= int(policy.max_frontier):
                        errors.append("frontier_capacity_rejected")
                        # Bounded discovery — stop expanding
                        break
                    memory_queue.append((link, depth + 1, url, 0))
                    queued_urls.add(link)
                    self._ensure_frontier_entry(
                        job_id=job_id,
                        tenant_id=tenant_id,
                        url=link,
                        canonical_url=link,
                        depth=depth + 1,
                        parent=url,
                        retry_count=0,
                        status=FRONTIER_PENDING,
                        cache=frontier_cache,
                    )

            checkpoint = CrawlCheckpoint(
                job_id=job_id,
                tenant_id=tenant_id,
                visited_count=len(visited),
                frontier_pending=len(memory_queue),
                pages_fetched=len(artifacts),
                pages_failed=pages_failed,
                pages_skipped=pages_skipped,
            )
            self._persist_checkpoint(checkpoint)

        if status == JOB_COMPLETED and pages_failed and artifacts:
            status = JOB_PARTIAL
        elif status == JOB_COMPLETED and not artifacts and pages_failed:
            status = JOB_FAILED

        checkpoint = CrawlCheckpoint(
            job_id=job_id,
            tenant_id=tenant_id,
            visited_count=len(visited),
            frontier_pending=len(memory_queue),
            pages_fetched=len(artifacts),
            pages_failed=pages_failed,
            pages_skipped=pages_skipped,
        )
        self._persist_checkpoint(checkpoint)

        return CrawlResult(
            artifacts=tuple(artifacts),
            visited=tuple(visited),
            skipped=tuple(skipped[:500]),
            errors=tuple(errors[:100]),
            resources=tuple(resources),
            checkpoint=checkpoint,
            status=status,
            pages_fetched=len(artifacts),
            pages_failed=pages_failed,
            pages_skipped=pages_skipped,
        )
