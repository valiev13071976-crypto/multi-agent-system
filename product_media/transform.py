"""Deterministic image transformations."""

from __future__ import annotations

import io
import uuid

from PIL import Image, ImageOps

from product_media.errors import MEDIA_TRANSFORM_INVALID, MediaError
from product_media.platform_models import ImageMetadata, TRANSFORM_PROFILE_VERSION, content_hash_bytes
from product_media.validation import detect_image_type, validate_and_extract_image


def _meta_from_image(img: Image.Image, fmt: str, mime: str, raw: bytes) -> ImageMetadata:
    return ImageMetadata(
        width=img.width,
        height=img.height,
        format=fmt,
        mime_type=mime,
        byte_size=len(raw),
        aspect_ratio=round(img.width / max(img.height, 1), 4),
        has_alpha=img.mode in ("RGBA", "LA", "PA"),
        color_mode=img.mode,
    )


def resize_image(
    data: bytes,
    *,
    width: int,
    height: int,
    fit: str = "contain",
    output_format: str | None = None,
) -> tuple[bytes, ImageMetadata]:
    validated = validate_and_extract_image(data)
    fmt, mime = detect_image_type(validated.data)
    out_fmt = output_format or fmt
    with Image.open(io.BytesIO(validated.canonical_data)) as img:
        if fit == "cover":
            img = ImageOps.fit(img, (width, height), method=Image.Resampling.LANCZOS)
        elif fit == "pad":
            img.thumbnail((width, height), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA" if img.mode in ("RGBA", "LA") else "RGB", (width, height), (255, 255, 255, 0))
            ox = (width - img.width) // 2
            oy = (height - img.height) // 2
            canvas.paste(img, (ox, oy))
            img = canvas
        else:
            img = img.copy()
            img.thumbnail((width, height), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        save_fmt = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}[out_fmt]
        img.save(buf, format=save_fmt)
        out = buf.getvalue()
        return out, _meta_from_image(img, out_fmt, mime, out)


def crop_image(data: bytes, *, left: int, top: int, width: int, height: int) -> tuple[bytes, ImageMetadata]:
    validated = validate_and_extract_image(data)
    fmt, mime = detect_image_type(validated.data)
    with Image.open(io.BytesIO(validated.canonical_data)) as img:
        if left < 0 or top < 0 or left + width > img.width or top + height > img.height:
            raise MediaError(MEDIA_TRANSFORM_INVALID, "crop out of bounds")
        cropped = img.crop((left, top, left + width, top + height))
        buf = io.BytesIO()
        save_fmt = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}[fmt]
        cropped.save(buf, format=save_fmt)
        out = buf.getvalue()
        return out, _meta_from_image(cropped, fmt, mime, out)


def strip_metadata(data: bytes) -> tuple[bytes, ImageMetadata]:
    validated = validate_and_extract_image(data)
    fmt, mime = detect_image_type(validated.data)
    with Image.open(io.BytesIO(validated.canonical_data)) as img:
        clean = Image.new(img.mode, img.size)
        clean.putdata(list(img.getdata()))
        buf = io.BytesIO()
        save_fmt = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}[fmt]
        clean.save(buf, format=save_fmt)
        out = buf.getvalue()
        return out, _meta_from_image(clean, fmt, mime, out)


def thumbnail_image(data: bytes, *, max_edge: int = 256) -> tuple[bytes, ImageMetadata]:
    return resize_image(data, width=max_edge, height=max_edge, fit="contain", output_format="jpeg")
