"""Batch admission for heavy media workloads."""

from __future__ import annotations

from product_media.errors import MEDIA_BATCH_REQUIRED, MediaBatchRequired
from product_media.policy import (
    MAX_BULK_IMAGE_COUNT,
    MAX_BULK_VIDEO_DURATION_SEC,
    MAX_SYNC_IMAGE_COUNT,
    MAX_SYNC_VARIANTS,
    MAX_SYNC_VIDEO_DURATION_SEC,
    MediaResourcePolicy,
)


def classify_media_workload(
    *,
    image_count: int = 0,
    total_bytes: int = 0,
    video_duration_sec: float = 0,
    variant_count: int = 0,
) -> str:
    if image_count > MAX_SYNC_IMAGE_COUNT:
        return "bulk"
    if variant_count > MAX_SYNC_VARIANTS:
        return "bulk"
    if video_duration_sec > MAX_SYNC_VIDEO_DURATION_SEC:
        return "bulk"
    if image_count > MAX_BULK_IMAGE_COUNT:
        return "heavy_bulk"
    if video_duration_sec > MAX_BULK_VIDEO_DURATION_SEC:
        return "heavy_bulk"
    return "sync"


def assert_sync_media_allowed(
    *,
    image_count: int = 0,
    total_bytes: int = 0,
    video_duration_sec: float = 0,
    variant_count: int = 0,
    interactive: bool = True,
    bulk: bool = False,
) -> None:
    if bulk:
        return
    workload = classify_media_workload(
        image_count=image_count,
        total_bytes=total_bytes,
        video_duration_sec=video_duration_sec,
        variant_count=variant_count,
    )
    if interactive and workload != "sync":
        raise MediaBatchRequired()


def assert_variant_limit(variant_count: int, policy: MediaResourcePolicy | None = None) -> None:
    policy = policy or MediaResourcePolicy()
    if variant_count > policy.max_sync_variants:
        raise MediaBatchRequired()
