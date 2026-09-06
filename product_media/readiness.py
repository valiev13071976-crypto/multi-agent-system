"""Safe, zero-network production readiness diagnostic for image generation.

Reports whether a real (non-fake) image-generation path can actually be
constructed from the current configuration, without making any provider
request. Never returns secret values -- only PRESENT/EMPTY for anything
that could be sensitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

READY = "READY"
NOT_READY = "NOT_READY"

_CHAT_IMAGE_GENERATE_TOOL_ID = "image.generate"
_MEDIA_ENDPOINT_PATH = "/api/v1/business-assistant/media/{version_id}"


@dataclass
class ImageGenerationReadiness:
    status: str
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons)}

    def __str__(self) -> str:
        lines = [f"IMAGE_GENERATION_READINESS={self.status}"]
        lines.extend(self.reasons)
        return "\n".join(lines)


def check_image_generation_readiness(
    *,
    env: dict,
    tool_registry=None,
    app=None,
) -> ImageGenerationReadiness:
    """Zero-network readiness check. Safe to call in production (e.g. from an
    operator diagnostic endpoint or startup log line) -- never calls the
    provider, never prints a secret value.
    """
    reasons: list[str] = []
    ready = True

    provider = str(env.get("MEDIA_IMAGE_PROVIDER") or "fake").strip().lower()
    reasons.append(f"MEDIA_IMAGE_PROVIDER={provider or 'EMPTY'}")
    if provider == "fake":
        reasons.append("MEDIA_IMAGE_PROVIDER=fake (placeholder images only, not a real provider)")
        ready = False

    key_present = bool(str(env.get("MEDIA_IMAGE_API_KEY") or env.get("OPENAI_API_KEY") or "").strip())
    reasons.append(f"OPENAI_API_KEY={'PRESENT' if key_present else 'EMPTY'}")
    if provider != "fake" and not key_present:
        ready = False

    model = str(env.get("MEDIA_IMAGE_MODEL") or "").strip()
    reasons.append(f"MEDIA_IMAGE_MODEL={'PRESENT' if model else 'EMPTY(defaults to dall-e-2)'}")

    db_path = str(env.get("PRODUCT_MEDIA_DB_PATH") or "").strip()
    reasons.append(f"MEDIA_STORAGE={'CONFIGURED(' + db_path + ')' if db_path else 'NOT_CONFIGURED(defaults to in-memory, non-durable)'}")
    if not db_path:
        ready = False

    if tool_registry is not None:
        try:
            registration = tool_registry.get_registration(_CHAT_IMAGE_GENERATE_TOOL_ID)
            descriptor_enabled = bool(getattr(registration.descriptor, "enabled", False))
            adapter_present = registration.adapter is not None
        except Exception:
            descriptor_enabled = False
            adapter_present = False
        reasons.append(f"IMAGE_GENERATE_CAPABILITY={'REGISTERED' if descriptor_enabled else 'NOT_REGISTERED'}")
        reasons.append(f"PRODUCT_MEDIA_ADAPTER={'REGISTERED' if adapter_present else 'NOT_REGISTERED'}")
        if not descriptor_enabled or not adapter_present:
            ready = False

    if app is not None:
        route_present = False
        try:
            for route in getattr(app, "routes", []):
                if getattr(route, "path", "") == _MEDIA_ENDPOINT_PATH:
                    route_present = True
                    break
        except Exception:
            route_present = False
        reasons.append(f"MEDIA_RETRIEVAL_ROUTE={'REGISTERED' if route_present else 'NOT_REGISTERED'}")
        if not route_present:
            ready = False

    return ImageGenerationReadiness(status=READY if ready else NOT_READY, reasons=tuple(reasons))
