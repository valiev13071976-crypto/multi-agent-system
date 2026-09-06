"""Production image generation adapter."""

from __future__ import annotations

import base64
import binascii
import io
import time
import uuid
from dataclasses import dataclass

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient
from integrations.production.observability import ProviderObservability
from product_media.errors import MEDIA_GENERATION_FAILED, MediaError
from product_media.providers.fake import FakeImageGenerationProvider, ProviderResult


@dataclass
class OpenAIImageGenerationProvider:
    api_key: str
    model: str = "dall-e-2"
    timeout_seconds: float = 120.0
    provider_id: str = "openai-image"
    obs: ProviderObservability | None = None
    _http: BoundedHttpClient | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ProductionProviderError(ProviderErrorCategory.CONFIGURATION_ERROR, message="image_key_missing", provider_id="media_image")
        self._http = BoundedHttpClient(provider_id="media_image", timeout_seconds=self.timeout_seconds)

    def generate(self, *, prompt: str, width: int = 512, height: int = 512, seed: int | None = None) -> ProviderResult:
        size = "512x512" if width <= 512 and height <= 512 else "1024x1024"
        started = time.monotonic()
        try:
            resp = self._http.request(
                "POST",
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json_body={
                    "model": self.model,
                    "prompt": prompt[:1000],
                    "size": size,
                    "n": 1,
                    # Without this, OpenAI's default response_format is a hosted "url" (no
                    # b64_json), which this adapter cannot ingest -- every real generation
                    # would otherwise fail with "empty_image" despite a successful HTTP 200.
                    "response_format": "b64_json",
                },
            )
            raw = self._extract_image_bytes(resp)
        except ProductionProviderError as exc:
            if self.obs:
                self.obs.emit(provider_id="media_image", operation="generate", success=False, error_category=exc.category.value)
            raise MediaError(MEDIA_GENERATION_FAILED, exc.message) from exc
        except MediaError:
            if self.obs:
                self.obs.emit(provider_id="media_image", operation="generate", success=False, error_category="invalid_response")
            raise
        if self.obs:
            self.obs.emit(provider_id="media_image", operation="generate", success=True, latency_ms=(time.monotonic() - started) * 1000)
        return ProviderResult(data=raw, mime_type="image/png", provider_id=self.provider_id, profile_version="1.0.0")

    def _extract_image_bytes(self, resp) -> bytes:
        """Normalize the OpenAI images.generate response into raw bytes.

        Every branch that cannot yield real, non-empty, decodable image bytes
        raises MediaError -- a provider-side HTTP 200 must never be treated as
        a successful generation unless it actually carries a usable image.
        """
        try:
            payload = resp.json()
        except Exception as exc:
            raise MediaError(MEDIA_GENERATION_FAILED, "malformed_response") from exc
        if not isinstance(payload, dict):
            raise MediaError(MEDIA_GENERATION_FAILED, "unsupported_response_shape")
        if payload.get("error"):
            raise MediaError(MEDIA_GENERATION_FAILED, "provider_error_payload")
        items = payload.get("data")
        if not isinstance(items, list) or not items:
            raise MediaError(MEDIA_GENERATION_FAILED, "empty_data")
        item = items[0]
        if not isinstance(item, dict):
            raise MediaError(MEDIA_GENERATION_FAILED, "unsupported_response_shape")
        b64 = item.get("b64_json")
        if not b64 or not isinstance(b64, str):
            raise MediaError(MEDIA_GENERATION_FAILED, "empty_image")
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MediaError(MEDIA_GENERATION_FAILED, "invalid_base64") from exc
        if not raw:
            raise MediaError(MEDIA_GENERATION_FAILED, "empty_image_bytes")
        return raw

    def health_check(self) -> dict:
        return {"status": "configured", "model": self.model}


def build_image_provider(env: dict):
    provider = str(env.get("MEDIA_IMAGE_PROVIDER") or "fake").strip().lower()
    if provider == "fake":
        return FakeImageGenerationProvider()
    key = str(env.get("MEDIA_IMAGE_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    if not key:
        # MEDIA_IMAGE_PROVIDER was explicitly set to a non-fake provider (e.g. "openai") --
        # a missing key must surface as a configuration error, not a silent fallback to the
        # fake/placeholder generator. Fake is only ever selected by explicit configuration
        # (the branch above), never as an implicit substitute for a real provider.
        raise ProductionProviderError(
            ProviderErrorCategory.CONFIGURATION_ERROR, message="image_key_required", provider_id="media_image"
        )
    return OpenAIImageGenerationProvider(api_key=key, model=str(env.get("MEDIA_IMAGE_MODEL") or "dall-e-2"))
