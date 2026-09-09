"""Top-level enrichment orchestration (the "target flow" from the module
docstring): identity -> research -> characteristics -> content -> media ->
``EnrichmentResult``. This is the single public entry point every caller
(tests, the ``business_assistant`` bridge, a future direct API) should use.
"""

from __future__ import annotations

from typing import Sequence

from product_enrichment.cache import EnrichmentCache
from product_enrichment.characteristics import bridge_characteristics_to_bitrix, merge_facts_into_characteristics
from product_enrichment.content import generate_content
from product_enrichment.identity import resolve_identity
from product_enrichment.media import MediaAcquisitionService
from product_enrichment.media_fetch import ImageFetchPort
from product_enrichment.models import (
    EnrichmentResult,
    IdentityConflictError,
    MediaCandidateInput,
    MediaResult,
    ProductIdentityQuery,
    SourceFact,
)
from product_enrichment.observability import (
    STAGE_CHARACTERISTICS_NORMALIZED,
    STAGE_CONTENT_PREPARED,
    STAGE_FAILED,
    STAGE_IDENTITY_FAILED,
    STAGE_IDENTITY_RESOLVED,
    STAGE_MEDIA_PROCESSED,
    STAGE_MEDIA_REJECTED,
    STAGE_PREVIEW_READY,
    STAGE_RESEARCH_COMPLETED,
    STAGE_RESEARCH_STARTED,
    EnrichmentObserver,
)
from product_enrichment.research import FetchTextPort, SearchPort, research_product


async def enrich_product(
    *,
    tenant_id: str,
    query: ProductIdentityQuery,
    search_port: SearchPort | None = None,
    fetch_port: FetchTextPort | None = None,
    media_fetcher: ImageFetchPort | None = None,
    media_candidates: Sequence[MediaCandidateInput] = (),
    extra_facts: Sequence[SourceFact] = (),
    cache: EnrichmentCache | None = None,
    observer: EnrichmentObserver | None = None,
) -> EnrichmentResult:
    """Runs the full enrichment pipeline for one product.

    ``search_port``/``fetch_port`` are optional -- when either is missing,
    research is skipped entirely (``research_available=False`` on the
    result) rather than raising, so this always still produces a usable
    (if incomplete) preview from ``extra_facts``/identity alone (module
    docstring requirement 15: "search unavailable -> do not hallucinate").
    ``media_fetcher``/``media_candidates`` are likewise optional; omitting
    them yields ``MediaResult(status="unresolved")``, never a fabricated
    image.
    """
    observer = observer or EnrichmentObserver()

    try:
        identity = resolve_identity(query)
    except IdentityConflictError as exc:
        observer.emit(STAGE_IDENTITY_FAILED, reason=exc.code)
        observer.emit(STAGE_FAILED, component="identity", reason=exc.code)
        raise
    observer.emit(STAGE_IDENTITY_RESOLVED, identity_key=identity.identity_key, strength=identity.strength)

    if cache is not None:
        cached = cache.get(tenant_id=tenant_id, identity_key=identity.identity_key)
        if cached is not None:
            observer.emit(STAGE_PREVIEW_READY, cache_hit=True)
            return EnrichmentResult(
                identity=cached.identity,
                characteristics=cached.characteristics,
                content=cached.content,
                media=cached.media,
                facts=cached.facts,
                conflicts=cached.conflicts,
                research_available=cached.research_available,
                cache_hit=True,
            )

    facts: tuple[SourceFact, ...] = tuple(extra_facts)
    research_available = search_port is not None and fetch_port is not None
    if research_available:
        observer.emit(STAGE_RESEARCH_STARTED, identity_key=identity.identity_key)
        try:
            researched = await research_product(identity, search_port=search_port, fetch_port=fetch_port, observer=observer)
        except Exception as exc:  # noqa: BLE001 -- a research failure degrades, never aborts enrichment
            observer.emit(STAGE_FAILED, component="research", reason=type(exc).__name__)
            researched = ()
        facts = facts + researched
        observer.emit(STAGE_RESEARCH_COMPLETED, fact_count=len(researched))

    characteristics, conflicts = merge_facts_into_characteristics(facts)
    characteristics = bridge_characteristics_to_bitrix(characteristics)
    observer.emit(STAGE_CHARACTERISTICS_NORMALIZED, count=len(characteristics), conflict_count=len(conflicts))

    content = generate_content(identity, characteristics)
    observer.emit(STAGE_CONTENT_PREPARED, facts_used=len(content.facts_used))

    media_result = MediaResult()
    if media_fetcher is not None and media_candidates:
        media_service = MediaAcquisitionService(fetcher=media_fetcher)
        try:
            media_result = await media_service.acquire(media_candidates, identity=identity)
        except Exception as exc:  # noqa: BLE001 -- media failures degrade to "unresolved", never abort enrichment
            observer.emit(STAGE_FAILED, component="media", reason=type(exc).__name__)
            media_result = MediaResult()
        for asset in media_result.assets:
            observer.emit(STAGE_MEDIA_PROCESSED, role=asset.role, content_hash=asset.content_hash)
        for rejected in media_result.rejected_candidates:
            observer.emit(STAGE_MEDIA_REJECTED, url=rejected.source_url, reason=rejected.reason)

    result = EnrichmentResult(
        identity=identity,
        characteristics=characteristics,
        content=content,
        media=media_result,
        facts=facts,
        conflicts=conflicts,
        research_available=research_available,
        cache_hit=False,
    )
    observer.emit(STAGE_PREVIEW_READY, cache_hit=False)
    if cache is not None:
        cache.put(tenant_id=tenant_id, identity_key=identity.identity_key, result=result)
    return result
