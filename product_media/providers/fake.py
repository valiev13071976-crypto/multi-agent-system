"""Deterministic fake media providers for tests and closure."""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass

from PIL import Image, ImageDraw

from product_media.errors import MEDIA_PROVIDER_UNAVAILABLE, MediaError


@dataclass
class ProviderResult:
    data: bytes
    mime_type: str
    provider_id: str
    profile_version: str


class FakeImageGenerationProvider:
    provider_id = "fake-image-gen"

    def generate(
        self,
        *,
        prompt: str,
        width: int = 512,
        height: int = 512,
        seed: int | None = None,
    ) -> ProviderResult:
        img = Image.new("RGB", (width, height), color=(40, 120, 200))
        draw = ImageDraw.Draw(img)
        label = (prompt or "generated")[:40]
        draw.text((10, 10), label, fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return ProviderResult(
            data=buf.getvalue(),
            mime_type="image/png",
            provider_id=self.provider_id,
            profile_version="1.0.0",
        )


class FakeBackgroundRemovalProvider:
    provider_id = "fake-bg-remove"

    def remove_background(self, *, source_bytes: bytes) -> ProviderResult:
        with Image.open(io.BytesIO(source_bytes)) as img:
            rgba = img.convert("RGBA")
            buf = io.BytesIO()
            rgba.save(buf, format="PNG")
            return ProviderResult(
                data=buf.getvalue(),
                mime_type="image/png",
                provider_id=self.provider_id,
                profile_version="1.0.0",
            )


class FakeImageEditProvider:
    provider_id = "fake-image-edit"

    def edit(self, *, source_bytes: bytes, instruction: str, mask_bytes: bytes | None = None) -> ProviderResult:
        if mask_bytes is not None:
            with Image.open(io.BytesIO(source_bytes)) as src, Image.open(io.BytesIO(mask_bytes)) as mask:
                if src.size != mask.size:
                    raise MediaError(MEDIA_PROVIDER_UNAVAILABLE, "mask dimension mismatch")
        with Image.open(io.BytesIO(source_bytes)) as img:
            edited = img.convert("RGB")
            draw = ImageDraw.Draw(edited)
            draw.text((8, 8), (instruction or "edit")[:30], fill=(255, 0, 0))
            buf = io.BytesIO()
            edited.save(buf, format="PNG")
            return ProviderResult(
                data=buf.getvalue(),
                mime_type="image/png",
                provider_id=self.provider_id,
                profile_version="1.0.0",
            )


class FailingVariantProvider(FakeImageGenerationProvider):
    def __init__(self, fail_on: set[int]):
        self.fail_on = fail_on
        self._calls = 0

    def generate(self, **kwargs) -> ProviderResult:
        self._calls += 1
        if self._calls in self.fail_on:
            raise MediaError(MEDIA_PROVIDER_UNAVAILABLE, "simulated failure")
        return super().generate(**kwargs)


class UnavailableProvider:
    provider_id = "unavailable"

    def generate(self, **kwargs):
        raise MediaError(MEDIA_PROVIDER_UNAVAILABLE)

    def remove_background(self, **kwargs):
        raise MediaError(MEDIA_PROVIDER_UNAVAILABLE)

    def edit(self, **kwargs):
        raise MediaError(MEDIA_PROVIDER_UNAVAILABLE)
