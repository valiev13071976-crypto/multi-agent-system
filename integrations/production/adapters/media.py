"""Production image generation adapter."""

from __future__ import annotations

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
                json_body={"model": self.model, "prompt": prompt[:1000], "size": size, "n": 1},
            )
            data = resp.json()
            import base64

            b64 = data.get("data", [{}])[0].get("b64_json")
            if not b64:
                raise MediaError(MEDIA_GENERATION_FAILED, "empty_image")
            raw = base64.b64decode(b64)
            if self.obs:
                self.obs.emit(provider_id="media_image", operation="generate", success=True, latency_ms=(time.monotonic() - started) * 1000)
            return ProviderResult(data=raw, mime_type="image/png", provider_id=self.provider_id, profile_version="1.0.0")
        except ProductionProviderError as exc:
            if self.obs:
                self.obs.emit(provider_id="media_image", operation="generate", success=False, error_category=exc.category.value)
            raise MediaError(MEDIA_GENERATION_FAILED, exc.message) from exc

    def health_check(self) -> dict:
        return {"status": "configured", "model": self.model}


def build_image_provider(env: dict):
    provider = str(env.get("MEDIA_IMAGE_PROVIDER") or "fake").strip().lower()
    if provider == "fake":
        return FakeImageGenerationProvider()
    key = str(env.get("MEDIA_IMAGE_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    prod = str(env.get("PANDA_ENV") or env.get("ENVIRONMENT") or "").strip().lower() in {"production", "prod"}
    if not key:
        if prod and provider != "fake":
            raise ProductionProviderError(ProviderErrorCategory.CONFIGURATION_ERROR, message="image_key_required", provider_id="media_image")
        return FakeImageGenerationProvider()
    return OpenAIImageGenerationProvider(api_key=key, model=str(env.get("MEDIA_IMAGE_MODEL") or "dall-e-2"))
