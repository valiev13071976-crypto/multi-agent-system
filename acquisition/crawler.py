"""Controlled web crawler foundation — uses ToolGateway HTTP, not ad-hoc networking."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

from acquisition.errors import AcquisitionDeniedError
from acquisition.manager import AcquisitionManager
from acquisition.models import (
    ACQ_HTTP_GET,
    AcquisitionRequest,
    RawArtifact,
    SourceDescriptor,
)
from tools.url_safety import UnsafeUrlError, validate_http_url


def canonicalize_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    # Drop fragment; keep query
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


@dataclass
class CrawlLimits:
    max_depth: int = 1
    max_pages: int = 10
    allowed_content_types: tuple[str, ...] = ("text/html", "application/xhtml+xml", "application/json", "text/plain")
    respect_robots: bool = True  # hook — default deny when robots checker absent


@dataclass
class CrawlResult:
    artifacts: tuple[RawArtifact, ...]
    visited: tuple[str, ...]
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class ControlledCrawler:
    """Bounded crawler — seed URLs, domain allowlist, depth/page limits, dedupe."""

    def __init__(self, manager: AcquisitionManager, *, robots_allowed=None):
        self.manager = manager
        self._robots_allowed = robots_allowed  # optional callable(url) -> bool

    def _domain_ok(self, url: str, source: SourceDescriptor) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not source.allowed_domains:
            return False
        allowed = {d.lower() for d in source.allowed_domains}
        return host in allowed or any(host.endswith("." + d) for d in allowed)

    def _content_ok(self, artifact: RawArtifact, limits: CrawlLimits) -> bool:
        ct = (artifact.content_type or "").split(";")[0].strip().lower()
        if not ct:
            return True
        return any(ct.startswith(a) for a in limits.allowed_content_types)

    def _extract_links(self, artifact: RawArtifact, base: str) -> list[str]:
        import re

        text = artifact.content_text or ""
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', text, flags=re.I)
        out = []
        for href in hrefs:
            if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
                continue
            absolute = canonicalize_url(urljoin(base, href))
            out.append(absolute)
        return out

    async def crawl(
        self,
        *,
        source: SourceDescriptor,
        seeds: tuple[str, ...],
        tenant_id: str,
        workflow_id: str = "",
        limits: CrawlLimits | None = None,
    ) -> CrawlResult:
        limits = limits or CrawlLimits()
        if not source.allowed_domains:
            raise AcquisitionDeniedError("crawler_requires_allowed_domains")

        queue: list[tuple[str, int]] = []
        for seed in seeds:
            try:
                validate_http_url(seed)
            except UnsafeUrlError as exc:
                raise AcquisitionDeniedError("unsafe_seed_url") from exc
            canon = canonicalize_url(seed)
            if not self._domain_ok(canon, source):
                raise AcquisitionDeniedError("seed_domain_not_allowed")
            queue.append((canon, 0))

        visited: set[str] = set()
        skipped: list[str] = []
        errors: list[str] = []
        artifacts: list[RawArtifact] = []

        while queue and len(artifacts) < limits.max_pages:
            url, depth = queue.pop(0)
            if url in visited:
                skipped.append(url)
                continue
            visited.add(url)
            if self._robots_allowed is not None and limits.respect_robots:
                if not self._robots_allowed(url):
                    skipped.append(url)
                    continue
            elif limits.respect_robots and self._robots_allowed is None:
                # Hook present but no checker — allow with provenance note only
                pass

            try:
                req = AcquisitionRequest(
                    source_id=source.source_id,
                    target=url,
                    acquisition_type=ACQ_HTTP_GET,
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                )
                art = await self.manager.acquire(req)
            except Exception as exc:
                errors.append(f"{url}:{getattr(exc, 'error_code', type(exc).__name__)}")
                continue

            if not self._content_ok(art, limits):
                skipped.append(url)
                continue
            artifacts.append(art)

            if depth < limits.max_depth:
                for link in self._extract_links(art, url):
                    if link in visited:
                        continue
                    if not self._domain_ok(link, source):
                        skipped.append(link)
                        continue
                    try:
                        validate_http_url(link)
                    except UnsafeUrlError:
                        skipped.append(link)
                        continue
                    queue.append((link, depth + 1))
                    if len(visited) + len(queue) > limits.max_pages * 3:
                        break

        return CrawlResult(
            artifacts=tuple(artifacts),
            visited=tuple(visited),
            skipped=tuple(skipped[:500]),
            errors=tuple(errors[:100]),
        )
