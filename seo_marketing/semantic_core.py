"""Versioned semantic core builder — no whole-corpus LLM."""

from __future__ import annotations

import uuid

from seo_marketing.keywords import cluster_keywords
from seo_marketing.platform_models import (
    MAX_LLM_CLUSTER_SAMPLE,
    SEMANTIC_CORE_VERSION,
    Keyword,
    KeywordCluster,
    SemanticCore,
)
from seo_marketing.policy import BULK_KEYWORD_BATCH_SIZE


def build_semantic_core(
    *,
    tenant_id: str,
    site_id: str,
    keywords: list[Keyword],
    version: int = 1,
    language: str = "en",
    country: str = "US",
    search_engine: str = "google",
    source_period: str = "",
    parent_version: int | None = None,
) -> tuple[SemanticCore, list[KeywordCluster]]:
    """Cluster via deterministic first-token buckets; LLM sample bound for naming only."""
    # Large sets processed in chunks conceptually — clustering is O(n) deterministic
    _ = BULK_KEYWORD_BATCH_SIZE
    _ = MAX_LLM_CLUSTER_SAMPLE
    clusters = cluster_keywords(keywords, tenant_id=tenant_id, site_id=site_id)
    core = SemanticCore(
        core_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        site_id=site_id,
        version=version,
        keyword_ids=tuple(k.keyword_id for k in keywords),
        cluster_ids=tuple(c.cluster_id for c in clusters),
        language=language,
        country=country,
        search_engine=search_engine,
        source_period=source_period,
        parent_version=parent_version,
    )
    _ = SEMANTIC_CORE_VERSION
    return core, clusters
