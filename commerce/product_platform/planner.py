"""Batch admission for heavy commerce workloads."""

from __future__ import annotations

from commerce.product_platform.errors import CommerceBatchRequired
from commerce.product_platform.policy import (
    MAX_BULK_IMPORT_ROWS,
    MAX_SYNC_CMS_WRITES,
    MAX_SYNC_ENRICH_COUNT,
    MAX_SYNC_IMPORT_ROWS,
    MAX_SYNC_REPRICE_COUNT,
)


def assert_sync_commerce_allowed(
    *,
    row_count: int = 0,
    enrich_count: int = 0,
    reprice_count: int = 0,
    cms_writes: int = 0,
    bulk: bool = False,
) -> None:
    if bulk:
        return
    if row_count > MAX_SYNC_IMPORT_ROWS:
        raise CommerceBatchRequired()
    if enrich_count > MAX_SYNC_ENRICH_COUNT:
        raise CommerceBatchRequired()
    if reprice_count > MAX_SYNC_REPRICE_COUNT:
        raise CommerceBatchRequired()
    if cms_writes > MAX_SYNC_CMS_WRITES:
        raise CommerceBatchRequired()


def classify_import_workload(row_count: int) -> str:
    if row_count > MAX_BULK_IMPORT_ROWS:
        return "heavy_bulk"
    if row_count > MAX_SYNC_IMPORT_ROWS:
        return "bulk"
    return "sync"
