"""Media type validation, metadata extraction, decompression bomb protection."""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass

from PIL import Image, ImageOps

from product_media.errors import (
    MEDIA_CORRUPT,
    MEDIA_PIXEL_LIMIT_EXCEEDED,
    MEDIA_TOO_LARGE,
    MEDIA_TYPE_MISMATCH,
    MEDIA_UNSUPPORTED,
    MediaError,
    MediaPixelLimitExceeded,
)
from product_media.platform_models import ImageMetadata
from product_media.policy import MediaResourcePolicy


_IMAGE_MAGICS = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpeg", "image/jpeg"),
    (b"RIFF", "webp", "image/webp"),  # RIFF....WEBP checked below
)


def detect_image_type(data: bytes) -> tuple[str, str]:
    if len(data) < 12:
        raise MediaError(MEDIA_CORRUPT)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    for magic, fmt, mime in _IMAGE_MAGICS:
        if fmt == "webp":
            continue
        if data.startswith(magic):
            return fmt, mime
    raise MediaError(MEDIA_UNSUPPORTED)


def _check_pixels(width: int, height: int, policy: MediaResourcePolicy) -> None:
    if width <= 0 or height <= 0:
        raise MediaError(MEDIA_CORRUPT, "invalid dimensions")
    if width > policy.max_sync_width or height > policy.max_sync_height:
        raise MediaError(MEDIA_PIXEL_LIMIT_EXCEEDED)
    pixels = width * height
    if pixels > policy.max_sync_pixels:
        raise MediaPixelLimitExceeded()


@dataclass
class ValidatedImage:
    data: bytes
    metadata: ImageMetadata
    canonical_data: bytes


def validate_and_extract_image(
    data: bytes,
    *,
    filename: str = "",
    declared_mime: str = "",
    policy: MediaResourcePolicy | None = None,
) -> ValidatedImage:
    policy = policy or MediaResourcePolicy()
    if len(data) > policy.max_sync_bytes:
        raise MediaError(MEDIA_TOO_LARGE)
    fmt, mime = detect_image_type(data)
    if declared_mime and declared_mime not in ("", mime, "application/octet-stream"):
        raise MediaError(MEDIA_TYPE_MISMATCH)
    ext = str(filename or "").lower()
    if ext.endswith(".png") and fmt != "png":
        raise MediaError(MEDIA_TYPE_MISMATCH)
    if ext.endswith((".jpg", ".jpeg")) and fmt != "jpeg":
        raise MediaError(MEDIA_TYPE_MISMATCH)
    if ext.endswith(".webp") and fmt != "webp":
        raise MediaError(MEDIA_TYPE_MISMATCH)

    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            _check_pixels(width, height, policy)
            # Decompression bomb: verify decoder agrees with header
            img.load()
            orientation = 1
            try:
                exif = img.getexif()
                orientation = int(exif.get(274, 1))
            except Exception:
                orientation = 1
            canonical = ImageOps.exif_transpose(img)
            out = io.BytesIO()
            save_fmt = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}[fmt]
            canonical.save(out, format=save_fmt)
            canonical_bytes = out.getvalue()
            meta = ImageMetadata(
                width=canonical.width,
                height=canonical.height,
                format=fmt,
                mime_type=mime,
                byte_size=len(data),
                aspect_ratio=round(canonical.width / max(canonical.height, 1), 4),
                has_alpha=canonical.mode in ("RGBA", "LA", "PA"),
                orientation=orientation,
                color_mode=canonical.mode,
            )
            return ValidatedImage(data=data, metadata=meta, canonical_data=canonical_bytes)
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError(MEDIA_CORRUPT, str(exc)) from exc


def synthetic_decompression_bomb_header() -> bytes:
    """PNG header claiming huge dimensions — safe to use in tests without allocating."""
    # IHDR chunk: width=65535 height=65535
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 65535, 65535, 8, 2, 0, 0, 0)
    ihdr_crc = b"\x00" * 4
    ihdr = b"IHDR" + ihdr_data + ihdr_crc
    ihdr_len = struct.pack(">I", len(ihdr_data))
    return sig + ihdr_len + ihdr + b"IEND" + struct.pack(">I", 0) + b"IEND" + b"\xaeB`\x82"
