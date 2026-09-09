"""Trusted product research (requirement 2), composed entirely from
EXISTING governed capabilities: ``tools.gateway.ToolGateway.search()`` for
keyword discovery and the ``scrape.fetch`` tool for page text -- see
``ToolGatewayResearchAdapter`` below, the only new "orchestration/adapter"
this module adds.

Fails SAFE, never hallucinates (requirement 15): any search/fetch
exception, or no search/fetch capability configured at all, yields an
empty evidence tuple -- callers must present "missing source data", never
fabricate a fact.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urljoin

from product_enrichment.characteristics import (
    extract_spec_lines,
    match_canonical_key,
    normalize_characteristic_value,
)
from product_enrichment.identity import detect_variant_conflict, evidence_matches_identity
from product_enrichment.models import (
    CONFIDENCE_PROBABLE,
    MediaCandidateInput,
    ResolvedIdentity,
    SOURCE_AUTHORIZED_DISTRIBUTOR,
    SOURCE_MANUFACTURER,
    SOURCE_MANUFACTURER_DOCUMENTATION,
    SOURCE_RETAIL_CATALOG,
    SOURCE_TRUST_RANK,
    SOURCE_UNKNOWN,
    SourceFact,
)
from product_enrichment.observability import STAGE_SOURCE_ACCEPTED, STAGE_SOURCE_REJECTED, EnrichmentObserver
from tools.url_safety import source_domain

# Minimal, generic brand -> manufacturer-domain heuristic (requirement 2
# source-priority tier 1). Extending to another brand is one more table
# entry -- never a new code path. A brand with no entry here simply never
# gets tier-1 (manufacturer) classification; its evidence can still be
# accepted at a lower trust tier.
_MANUFACTURER_DOMAINS: dict[str, tuple[str, ...]] = {
    "lg": ("lg.com",),
    "samsung": ("samsung.com",),
    "sony": ("sony.com", "sony.net"),
    "philips": ("philips.com",),
    "xiaomi": ("mi.com", "xiaomi.com"),
    "haier": ("haier.com",),
    "tcl": ("tcl.com",),
    "hisense": ("hisense.com",),
}
_AUTHORIZED_DISTRIBUTOR_DOMAINS = ("citilink.ru", "mvideo.ru", "dns-shop.ru", "eldorado.ru")

# Best-effort image-candidate discovery from an already fetched,
# identity-verified page (requirement 7's "MEDIA GAP" -- Brave is a web
# search API, it has no image endpoint this pipeline uses, so the ONLY
# source of a candidate product-image URL is a page ``scrape.fetch``
# already downloaded and this module already accepted for CHARACTERISTIC
# extraction). og:image is checked first (usually the single canonical
# product photo a manufacturer/retailer page declares), then <img> tags,
# in document order. This NEVER invents a URL -- every candidate returned
# was literally present in the fetched page's own markup; validation/SSRF
# checks happen later, in GovernedImageFetcher / MediaAcquisitionService,
# exactly like every other externally sourced URL in this pipeline.
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
_IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

MAX_IMAGE_CANDIDATES_PER_PAGE = 3
MAX_MEDIA_CANDIDATES_PER_RUN = 8


def extract_image_candidate_urls(html_text: str, *, base_url: str) -> tuple[str, ...]:
    """Best-effort, non-hallucinating extraction of product-image URLs
    embedded in one fetched page. Relative URLs are resolved against
    ``base_url``; ``data:`` URIs and anything that fails to resolve to an
    absolute ``http(s)`` URL are dropped (they can never be handed to
    ``GovernedImageFetcher`` anyway). Bounded to
    ``MAX_IMAGE_CANDIDATES_PER_PAGE`` per page (cost control)."""
    if not html_text:
        return ()
    found: list[str] = []
    for pattern in (_OG_IMAGE_RE, _IMG_TAG_RE):
        for match in pattern.finditer(html_text):
            raw = match.group(1).strip()
            if not raw or raw.startswith("data:"):
                continue
            try:
                resolved = urljoin(base_url, raw)
            except ValueError:
                continue
            if not resolved.lower().startswith(("http://", "https://")):
                continue
            if resolved in found:
                continue
            found.append(resolved)
            if len(found) >= MAX_IMAGE_CANDIDATES_PER_PAGE:
                return tuple(found)
    return tuple(found)


def classify_source_type(url: str, *, brand: str) -> str:
    domain = source_domain(url)
    if not domain:
        return SOURCE_UNKNOWN
    manufacturer_domains = _MANUFACTURER_DOMAINS.get(brand.strip().casefold(), ())
    if any(domain == d or domain.endswith(f".{d}") for d in manufacturer_domains):
        if domain.endswith((".pdf",)) or "manual" in url.casefold() or "spec" in url.casefold():
            return SOURCE_MANUFACTURER_DOCUMENTATION
        return SOURCE_MANUFACTURER
    if any(domain == d or domain.endswith(f".{d}") for d in _AUTHORIZED_DISTRIBUTOR_DOMAINS):
        return SOURCE_AUTHORIZED_DISTRIBUTOR
    return SOURCE_RETAIL_CATALOG


class SearchPort(Protocol):
    async def search(self, query: str, max_results: int = 5): ...


class FetchTextPort(Protocol):
    async def fetch_text(self, url: str) -> str: ...


class ToolGatewayResearchAdapter:
    """Adapts the existing, already-governed ``ToolGateway`` into the two
    narrow ports this module needs -- no second search/fetch
    infrastructure. ``search`` reuses ``ToolGateway.search()`` directly;
    ``fetch_text`` invokes the registered ``scrape.fetch`` tool exactly the
    way ``acquisition.web_source._invoke_scrape_fetch`` already does."""

    def __init__(self, tool_gateway, *, tenant_id: str, workflow_id: str = "product-enrichment"):
        self._gateway = tool_gateway
        self._tenant_id = tenant_id
        self._workflow_id = workflow_id

    async def search(self, query: str, max_results: int = 5):
        return await self._gateway.search(query, max_results=max_results)

    async def fetch_text(self, url: str) -> str:
        # Mirrors acquisition.web_source._invoke_scrape_fetch's own
        # invocation shape EXACTLY -- ToolGateway.invoke()'s capability
        # check (_require_capabilities) denies scrape.fetch's
        # (CAP_SCRAPE, CAP_EXTERNAL_READ) requirement whenever neither
        # ToolRequest.requested_capabilities nor a granting CapabilitySet
        # is supplied. Omitting either (as an earlier version of this
        # adapter did) made every real fetch_text() call silently return
        # "" against a REAL, capability-enforcing ToolGateway -- research
        # would then report every result as "empty_page" even with a
        # working search backend and a live scrape.fetch tool.
        from autonomy.capabilities import CAP_EXTERNAL_READ, CAP_SCRAPE, CapabilitySet
        from autonomy.models import utc_now
        from tools.models import ToolRequest

        caps = (CAP_SCRAPE, CAP_EXTERNAL_READ)
        request = ToolRequest(
            request_id=str(uuid.uuid4()),
            workflow_id=self._workflow_id,
            task_id=str(uuid.uuid4()),
            tool_id="scrape.fetch",
            operation="fetch",
            arguments={"url": url},
            tenant_id=self._tenant_id,
            requested_capabilities=caps,
        )
        result = await self._gateway.invoke(
            request,
            capabilities=CapabilitySet(
                subject_id="product_enrichment", capabilities=caps, issued_at=utc_now()
            ),
        )
        if not getattr(result, "success", False):
            return ""
        return str((getattr(result, "data", None) or {}).get("body_text") or "")


async def research_product(
    identity: ResolvedIdentity,
    *,
    search_port: SearchPort,
    fetch_port: FetchTextPort,
    max_sources: int = 5,
    observer: EnrichmentObserver | None = None,
    media_sink: list | None = None,
) -> tuple[SourceFact, ...]:
    """Fails SAFE (requirement 15): returns an empty tuple on any
    search/fetch failure rather than raising -- the caller must treat that
    as "missing source data", never as license to invent facts.

    ``media_sink``, if given, is a plain list this function APPENDS
    ``MediaCandidateInput`` entries to (never replaces/clears it) --
    image URLs discovered in the SAME already identity-verified, non-
    conflicting fetched pages used for characteristic extraction above.
    Optional and additive: omitting it (the default) reproduces the exact
    prior behavior/return value for every existing caller."""
    observer = observer or EnrichmentObserver()
    query = f"{identity.brand} {identity.model} характеристики specifications"
    try:
        results = await search_port.search(query, max_results=max_sources)
    except Exception:  # noqa: BLE001 -- search unavailable is a normal, expected outcome
        return ()

    facts: list[SourceFact] = []
    discovered_media: list[MediaCandidateInput] = []
    seen_media_urls: set[str] = set()
    retrieved_at = datetime.now(timezone.utc).isoformat()
    for result in results or []:
        url = str(getattr(result, "url", "") or "")
        title = str(getattr(result, "title", "") or "")
        snippet = str(getattr(result, "snippet", "") or "")
        if not evidence_matches_identity(identity, text=f"{title} {snippet}", url=url):
            observer.emit(STAGE_SOURCE_REJECTED, url=url, reason="identity_not_confirmed")
            continue
        conflict_code = detect_variant_conflict(identity, text=f"{title} {snippet}")
        if conflict_code:
            observer.emit(STAGE_SOURCE_REJECTED, url=url, reason=conflict_code)
            continue

        source_type = classify_source_type(url, brand=identity.brand)
        try:
            page_text = await fetch_port.fetch_text(url)
        except Exception:  # noqa: BLE001 -- one failed fetch must not abort the whole run
            observer.emit(STAGE_SOURCE_REJECTED, url=url, reason="fetch_failed")
            continue
        if not page_text:
            observer.emit(STAGE_SOURCE_REJECTED, url=url, reason="empty_page")
            continue

        if media_sink is not None and len(discovered_media) < MAX_MEDIA_CANDIDATES_PER_RUN:
            for image_url in extract_image_candidate_urls(page_text, base_url=url):
                if image_url in seen_media_urls:
                    continue
                seen_media_urls.add(image_url)
                discovered_media.append(MediaCandidateInput(url=image_url, source_type=source_type))

        domain = source_domain(url)
        accepted_any = False
        for label, raw_value in extract_spec_lines(page_text):
            key = match_canonical_key(label)
            if key is None:
                continue
            page_conflict = detect_variant_conflict(identity, text=raw_value)
            if page_conflict:
                continue
            normalized_value, unit = normalize_characteristic_value(key, raw_value)
            if not normalized_value:
                continue
            facts.append(
                SourceFact(
                    characteristic_key=key,
                    raw_label=label,
                    raw_value=raw_value,
                    normalized_value=normalized_value,
                    unit=unit,
                    source_url=url,
                    source_type=source_type,
                    source_domain=domain,
                    confidence=CONFIDENCE_PROBABLE,
                    retrieved_at=retrieved_at,
                )
            )
            accepted_any = True
        observer.emit(
            STAGE_SOURCE_ACCEPTED if accepted_any else STAGE_SOURCE_REJECTED,
            url=url,
            reason="" if accepted_any else "no_recognizable_characteristics",
        )
    if media_sink is not None and discovered_media:
        # Requirement 5's source-priority order (manufacturer /
        # manufacturer docs first, retail catalog last) -- ordering in the
        # sequence handed to MediaAcquisitionService encodes preference;
        # it picks candidates[0] as the preview/detail master.
        discovered_media.sort(key=lambda c: -SOURCE_TRUST_RANK.get(c.source_type, 0))
        media_sink.extend(discovered_media[:MAX_MEDIA_CANDIDATES_PER_RUN])
    return tuple(facts)
