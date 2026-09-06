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
from product_media.errors import (
    MEDIA_GENERATION_FAILED,
    STAGE_DECODE,
    STAGE_PROVIDER_REQUEST,
    STAGE_PROVIDER_RESPONSE,
    MediaError,
)
from product_media.providers.fake import FakeImageGenerationProvider, ProviderResult

# OpenAI images.generate size enums per model family. dall-e-2 is square-only;
# dall-e-3 and gpt-image-* reject sizes outside their own enum with HTTP 400.
# "1024x1024" is the one value valid across every currently supported model,
# used as the safe square default when the aspect ratio hint is square/unknown.
_DALLE2_SIZES = ("256x256", "512x512", "1024x1024")

# gpt-image-* models do not accept response_format at all (HTTP 400 "Unknown
# parameter: 'response_format'") -- they always return b64_json. Only the
# legacy dall-e-* models accept/require it to get b64_json instead of a
# hosted "url" (which this adapter cannot ingest).
_RESPONSE_FORMAT_MODEL_PREFIXES = ("dall-e",)


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

    def _resolve_size(self, width: int, height: int) -> str:
        model = str(self.model or "").strip().lower()
        landscape = width > height
        portrait = height > width
        if model.startswith("dall-e-2"):
            largest = max(int(width or 0), int(height or 0))
            for candidate in _DALLE2_SIZES:
                edge = int(candidate.split("x")[0])
                if largest <= edge:
                    return candidate
            return _DALLE2_SIZES[-1]
        if model.startswith("dall-e-3"):
            if landscape:
                return "1792x1024"
            if portrait:
                return "1024x1792"
            return "1024x1024"
        # gpt-image-* and any other/unrecognized model identifier: use the
        # gpt-image size enum, the current OpenAI flagship image model family.
        if landscape:
            return "1536x1024"
        if portrait:
            return "1024x1536"
        return "1024x1024"

    def _supports_response_format(self) -> bool:
        model = str(self.model or "").strip().lower()
        return model.startswith(_RESPONSE_FORMAT_MODEL_PREFIXES)

    def generate(self, *, prompt: str, width: int = 512, height: int = 512, seed: int | None = None) -> ProviderResult:
        started = time.monotonic()
        json_body = {
            "model": self.model,
            "prompt": prompt[:1000],
            "size": self._resolve_size(width, height),
            "n": 1,
        }
        if self._supports_response_format():
            # Without this, dall-e-2/dall-e-3's default response_format is a hosted "url"
            # (no b64_json), which this adapter cannot ingest. gpt-image-* models reject
            # this parameter outright (HTTP 400 "Unknown parameter") -- they always return
            # b64_json, so it must be omitted for them.
            json_body["response_format"] = "b64_json"
        try:
            resp = self._http.request(
                "POST",
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json_body=json_body,
            )
            raw = self._extract_image_bytes(resp)
        except ProductionProviderError as exc:
            if self.obs:
                self.obs.emit(provider_id="media_image", operation="generate", success=False, error_category=exc.category.value)
            raise MediaError(MEDIA_GENERATION_FAILED, exc.message, stage=STAGE_PROVIDER_REQUEST) from exc
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
            raise MediaError(MEDIA_GENERATION_FAILED, "malformed_response", stage=STAGE_PROVIDER_RESPONSE) from exc
        if not isinstance(payload, dict):
            raise MediaError(MEDIA_GENERATION_FAILED, "unsupported_response_shape", stage=STAGE_PROVIDER_RESPONSE)
        if payload.get("error"):
            raise MediaError(MEDIA_GENERATION_FAILED, "provider_error_payload", stage=STAGE_PROVIDER_RESPONSE)
        items = payload.get("data")
        if not isinstance(items, list) or not items:
            raise MediaError(MEDIA_GENERATION_FAILED, "empty_data", stage=STAGE_PROVIDER_RESPONSE)
        item = items[0]
        if not isinstance(item, dict):
            raise MediaError(MEDIA_GENERATION_FAILED, "unsupported_response_shape", stage=STAGE_PROVIDER_RESPONSE)
        b64 = item.get("b64_json")
        if not b64 or not isinstance(b64, str):
            raise MediaError(MEDIA_GENERATION_FAILED, "empty_image", stage=STAGE_PROVIDER_RESPONSE)
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MediaError(MEDIA_GENERATION_FAILED, "invalid_base64", stage=STAGE_DECODE) from exc
        if not raw:
            raise MediaError(MEDIA_GENERATION_FAILED, "empty_image_bytes", stage=STAGE_DECODE)
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
