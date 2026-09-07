"""Ephemeral general-web source registration + robots.txt priming (Block 5.2).

The Data Acquisition Platform is built around pre-registered, trusted
``SourceDefinition``s (supplier/marketplace/competitor feeds, etc). Ordinary
chat requests reference an arbitrary public URL the user just pasted, so this
module bridges that gap: it derives a deterministic, tenant-scoped
``SourceDefinition`` for the URL's host (trust level ``TRUST_GENERAL_WEB``,
host-restricted, ``tool_id="scrape.fetch"``) and registers it idempotently —
reusing the existing ``AcquisitionManager`` / ``ControlledCrawler`` machinery
unchanged rather than inventing a second acquisition path for ad-hoc URLs.
"""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import urlparse

from acquisition.errors import AcquisitionDeniedError, SourceAlreadyRegisteredError
from acquisition.models import (
    ACQ_HTTP_GET,
    SOURCE_WEBSITE,
    TRUST_GENERAL_WEB,
    CrawlPolicy,
    SourceDefinition,
)
from acquisition.registry import SourceNotFoundError
from security.tenant import require_tenant_id
from tools.url_safety import UnsafeUrlError, validate_http_url

EPHEMERAL_SOURCE_PREFIX = "web-adhoc-"


def ephemeral_source_id(tenant_id: str, host: str) -> str:
    """Deterministic id — same tenant+host always maps to the same source,
    so repeated requests to the same site register at most once."""

    digest = hashlib.sha256(f"{tenant_id}:{host}".encode("utf-8")).hexdigest()[:24]
    return f"{EPHEMERAL_SOURCE_PREFIX}{digest}"


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def get_or_create_ephemeral_source(
    service,
    *,
    tenant_id: str,
    url: str,
    max_pages: int = 1,
    max_depth: int = 0,
) -> SourceDefinition:
    """Return a tenant-scoped, host-restricted ephemeral ``SourceDefinition``
    for ``url``'s host, registering it on first use. Never widens beyond a
    single host, never persists credentials."""

    tid = require_tenant_id(tenant_id)
    try:
        safe_url = validate_http_url(url)
    except UnsafeUrlError as exc:
        raise AcquisitionDeniedError("unsafe_seed_url") from exc
    host = host_of(safe_url)
    if not host:
        raise AcquisitionDeniedError("unsafe_seed_url")

    source_id = ephemeral_source_id(tid, host)
    try:
        existing = service.sources.get(source_id, tenant_id=tid)
        return SourceDefinition.from_descriptor(existing, seed_urls=(safe_url,))
    except SourceNotFoundError:
        pass

    definition = SourceDefinition(
        source_id=source_id,
        source_type=SOURCE_WEBSITE,
        tenant_id=tid,
        trust_level=TRUST_GENERAL_WEB,
        allowed_hosts=(host,),
        seed_urls=(safe_url,),
        tool_id="scrape.fetch",
        crawl_policy=CrawlPolicy(max_depth=max_depth, max_pages=max_pages, respect_robots=True),
        name=f"web:{host}",
        metadata={"ephemeral": True},
    )
    try:
        service.register_source_definition(definition)
    except SourceAlreadyRegisteredError:
        pass
    return definition


async def _invoke_scrape_fetch(gateway, *, tenant_id: str, url: str, workflow_id: str = ""):
    from autonomy.capabilities import CAP_EXTERNAL_READ, CAP_SCRAPE, CapabilitySet
    from autonomy.models import utc_now as a_now
    from tools.models import ToolRequest

    caps = (CAP_SCRAPE, CAP_EXTERNAL_READ)
    request = ToolRequest(
        request_id=str(uuid.uuid4()),
        workflow_id=workflow_id or "acquisition",
        task_id="robots",
        tool_id="scrape.fetch",
        operation=ACQ_HTTP_GET.split("_")[-1],  # "get"
        arguments={"url": url},
        tenant_id=tenant_id,
        requested_capabilities=caps,
    )
    return await gateway.invoke(
        request,
        capabilities=CapabilitySet(subject_id="acquisition", capabilities=caps, issued_at=a_now()),
    )


async def load_robots_for_host(
    service, *, tenant_id: str, host: str, workflow_id: str = ""
) -> bool:
    """Best-effort robots.txt fetch through the governed ``scrape.fetch``
    tool, primed into the shared ``RobotsPolicy`` cache. Missing/blocked
    robots.txt degrades to permissive (matches ``RobotsPolicy(fail_closed=False)``
    default already used by ``AcquisitionService``) — never raises."""

    if getattr(service, "gateway", None) is None:
        return False
    robots_url = f"https://{host}/robots.txt"
    try:
        result = await _invoke_scrape_fetch(
            service.gateway, tenant_id=tenant_id, url=robots_url, workflow_id=workflow_id
        )
    except Exception:
        return False
    if not bool(getattr(result, "success", False)):
        service.robots.load(tenant_id, host, "")
        return True
    data = dict(getattr(result, "data", None) or {})
    status = int(data.get("status_code") or 0)
    if status and status >= 400:
        service.robots.load(tenant_id, host, "")
        return True
    body = str(data.get("body_text") or "")
    service.robots.load(tenant_id, host, body)
    return True
