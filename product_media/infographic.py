"""Fact-locked product infographic compositor."""

from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw

from product_media.errors import MEDIA_FACT_UNSUPPORTED, MEDIA_TEMPLATE_INVALID, MEDIA_TEXT_OVERFLOW, MediaError
from product_media.platform_models import MediaTemplate

_INVENTED_CLAIM = re.compile(
    r"(?i)\b(certif(?:ied|ication)|warranty\s+\d+|discount\s+\d+%|in\s+stock\s+\d+|rating\s+\d\.\d)\b"
)


def default_infographic_template(*, tenant_id: str, width: int = 1000, height: int = 1000) -> MediaTemplate:
    return MediaTemplate(
        template_id="infographic_v1",
        tenant_id=tenant_id,
        version="1.0.0",
        canvas_width=width,
        canvas_height=height,
        product_zone={"x": 100, "y": 80, "w": 800, "h": 600},
        text_zones=(
            {"id": "title", "x": 50, "y": 720, "w": 900, "h": 60, "overflow": "truncate"},
            {"id": "facts", "x": 50, "y": 800, "w": 900, "h": 120, "overflow": "wrap"},
        ),
    )


def render_infographic(
    *,
    product_image: bytes,
    template: MediaTemplate,
    product_facts: dict[str, str],
    title: str = "",
) -> bytes:
    if template.canvas_width <= 0 or template.canvas_height <= 0:
        raise MediaError(MEDIA_TEMPLATE_INVALID, "invalid_canvas")
    # Fact lock: only render supplied fact keys; reject invented claim patterns in free text
    if title and _INVENTED_CLAIM.search(title):
        raise MediaError(MEDIA_FACT_UNSUPPORTED, "invented_claim_in_title")
    for key, value in product_facts.items():
        if key in {"price", "stock", "discount", "warranty", "certification"} and not str(value).strip():
            raise MediaError(MEDIA_FACT_UNSUPPORTED, f"empty_fact:{key}")
        if _INVENTED_CLAIM.search(str(value)):
            # Allowed only when the fact key itself is that field from governed source
            if key not in {"warranty", "discount", "stock", "certification", "rating"}:
                raise MediaError(MEDIA_FACT_UNSUPPORTED, f"invented_claim:{key}")

    canvas = Image.new("RGB", (template.canvas_width, template.canvas_height), (255, 255, 255))
    with Image.open(io.BytesIO(product_image)) as src:
        zone = template.product_zone
        zx, zy = int(zone.get("x", 0)), int(zone.get("y", 0))
        zw, zh = int(zone.get("w", src.width)), int(zone.get("h", src.height))
        product = src.convert("RGBA")
        product.thumbnail((zw, zh), Image.Resampling.LANCZOS)
        canvas.paste(product, (zx, zy), product if product.mode == "RGBA" else None)

    draw = ImageDraw.Draw(canvas)
    fact_line = " | ".join(f"{k}: {v}" for k, v in sorted(product_facts.items()) if k != "name")
    title_text = title or str(product_facts.get("name") or "Product")

    for zone in template.text_zones:
        zid = str(zone.get("id") or "")
        text = title_text if zid == "title" else fact_line
        overflow = str(zone.get("overflow") or "truncate")
        max_chars = max(8, int(zone.get("w", 100)) // 8)
        if len(text) > max_chars:
            if overflow == "reject":
                raise MediaError(MEDIA_TEXT_OVERFLOW, zid)
            if overflow == "truncate":
                text = text[: max_chars - 1] + "…"
            # wrap: keep truncated for deterministic fake compositor
            elif overflow == "wrap":
                text = text[:max_chars]
        x, y = int(zone.get("x", 0)), int(zone.get("y", 0))
        draw.text((x, y), text, fill=(20, 20, 20))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
