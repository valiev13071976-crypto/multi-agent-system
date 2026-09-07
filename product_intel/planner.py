"""Product planner — bounded sync admission for heavy catalog workloads.

Mirrors ``content_intel.planner``/``data_intel.large`` (Block 5.3/5.1): small
product operations execute inline; large catalog import/matching/enrichment
must be routed through the existing batch/background runtime (spec section
21) rather than a new product-specific job queue.
"""

from __future__ import annotations

from product_intel.errors import ProductBatchRequired

LARGE_SYNC_ITEMS = 25
LARGE_BATCH_ITEMS = 200


def _must_batch(*, item_count: int | None, bulk: bool) -> bool:
    count = int(item_count) if item_count is not None else 0
    if bulk:
        return False
    return count >= LARGE_SYNC_ITEMS


def assert_sync_product_allowed(*, item_count: int = 1, bulk: bool = False) -> None:
    if _must_batch(item_count=item_count, bulk=bulk):
        raise ProductBatchRequired()


def workload_class_for(item_count: int) -> str:
    """Best-effort label reused by observability/tool metadata (Block 3 reuse)."""
    if item_count >= LARGE_BATCH_ITEMS:
        return "batch"
    if item_count >= LARGE_SYNC_ITEMS:
        return "batch"
    return "interactive"
