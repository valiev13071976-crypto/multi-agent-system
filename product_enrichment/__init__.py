"""Panda product enrichment pipeline.

Turns basic supplier commercial data (brand, model/article, EAN, category,
purchase price -- exactly what ``data_intel``'s XLSX row lookup already
produces) into a COMPLETE canonical product record (verified
characteristics, fact-only generated content, downloaded/validated/
processed product media, SEO metadata) BEFORE the existing governed Bitrix
preview/write path (``business_assistant.controlled_bitrix_write``) ever
runs.

This is deliberately NOT a new architecture. It composes EXISTING Panda
capabilities:

- governed search/fetch (``tools.gateway.ToolGateway`` -- ``search`` +
  ``scrape.fetch``) for trusted product research (module: ``research``);
- the verified Bitrix characteristic resolver added in the "complete
  product card" pass (``integrations.bitrix.schema``) for characteristic
  -> Bitrix-property mapping (module: ``characteristics``);
- the Product Media Intelligence platform's validation/transform/hashing
  primitives (``product_media.validation``/``product_media.transform``/
  ``product_media.platform_models``) for image validation, dedup and
  destination-specific processing (module: ``media``);
- the SAME SSRF-safe URL validator ``tools.platform.web_fetch_adapter``
  (``scrape.fetch``) uses (``tools.url_safety.validate_http_url``) for a
  governed *binary* image download (module: ``media_fetch`` -- the one
  genuinely missing adapter: ``scrape.fetch`` only returns decoded text,
  which corrupts binary image content).

Only new orchestration/adapters are added; no existing write path,
approval gate, or connector is replaced or duplicated. See
``docs/product-enrichment-pipeline.md`` for the full contract.
"""

from product_enrichment.models import (
    ContentDraft,
    EnrichmentResult,
    IdentityConflictError,
    MediaAsset,
    MediaCandidateInput,
    MediaResult,
    NormalizedCharacteristic,
    ProductIdentityQuery,
    ResolvedIdentity,
    SourceFact,
)
from product_enrichment.orchestrator import enrich_product

__all__ = [
    "ContentDraft",
    "EnrichmentResult",
    "IdentityConflictError",
    "MediaAsset",
    "MediaCandidateInput",
    "MediaResult",
    "NormalizedCharacteristic",
    "ProductIdentityQuery",
    "ResolvedIdentity",
    "SourceFact",
    "enrich_product",
]
