"""Central media resource limits (Block 10)."""

from __future__ import annotations

from dataclasses import dataclass

# Sync bounds
MAX_SYNC_BYTES = 8 * 1024 * 1024  # 8 MiB
MAX_SYNC_PIXELS = 16_000_000  # 16 MP
MAX_SYNC_WIDTH = 8192
MAX_SYNC_HEIGHT = 8192
MAX_SYNC_IMAGE_COUNT = 10
MAX_SYNC_VARIANTS = 4

# Bulk bounds
MAX_BULK_IMAGE_COUNT = 100
MAX_BULK_BYTES = 256 * 1024 * 1024

# Video baseline
MAX_SYNC_VIDEO_BYTES = 50 * 1024 * 1024
MAX_SYNC_VIDEO_DURATION_SEC = 120
MAX_SYNC_VIDEO_FRAMES = 30
MAX_BULK_VIDEO_DURATION_SEC = 600

# Similarity
MAX_SIMILARITY_CANDIDATES = 50

# Profile version
MEDIA_POLICY_VERSION = "1.0.0"


@dataclass(frozen=True)
class MediaResourcePolicy:
    max_sync_bytes: int = MAX_SYNC_BYTES
    max_sync_pixels: int = MAX_SYNC_PIXELS
    max_sync_width: int = MAX_SYNC_WIDTH
    max_sync_height: int = MAX_SYNC_HEIGHT
    max_sync_image_count: int = MAX_SYNC_IMAGE_COUNT
    max_sync_variants: int = MAX_SYNC_VARIANTS
    max_bulk_image_count: int = MAX_BULK_IMAGE_COUNT
    policy_version: str = MEDIA_POLICY_VERSION
