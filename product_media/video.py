"""Product video recipe + governed fake renderer boundary."""

from __future__ import annotations

import hashlib
import uuid

from product_media.errors import MEDIA_RECIPE_INVALID, MEDIA_RIGHTS_DENIED, MEDIA_VIDEO_RENDER_FAILED, MediaError
from product_media.platform_models import (
    RIGHTS_OWNED,
    RIGHTS_THIRD_PARTY_RESTRICTED,
    RIGHTS_UNKNOWN,
    VIDEO_PROFILE_VERSION,
    VideoRecipe,
    VideoScene,
)


def build_video_recipe(
    *,
    tenant_id: str,
    scenes: list[dict],
    aspect_ratio: str = "9:16",
    duration_sec: float = 15.0,
    media_brief_id: str = "",
    rights_status: str = RIGHTS_UNKNOWN,
    audio_refs: tuple[str, ...] = (),
) -> VideoRecipe:
    if not scenes:
        raise MediaError(MEDIA_RECIPE_INVALID, "empty_scenes")
    built: list[VideoScene] = []
    for row in scenes:
        # No executable timeline scripts
        for banned in ("eval", "exec", "code", "script"):
            if banned in row:
                raise MediaError(MEDIA_RECIPE_INVALID, f"banned_timeline:{banned}")
        start = float(row.get("start_sec") or 0)
        end = float(row.get("end_sec") or start + 1)
        if end <= start:
            raise MediaError(MEDIA_RECIPE_INVALID, "invalid_scene_window")
        built.append(
            VideoScene(
                scene_id=str(row.get("scene_id") or uuid.uuid4()),
                start_sec=start,
                end_sec=end,
                source_version_id=str(row.get("source_version_id") or ""),
                text_overlay=str(row.get("text_overlay") or "")[:120],
                transition=str(row.get("transition") or "cut"),
            )
        )
    return VideoRecipe(
        recipe_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        version=VIDEO_PROFILE_VERSION,
        scenes=tuple(built),
        aspect_ratio=aspect_ratio,
        duration_sec=float(duration_sec),
        media_brief_id=media_brief_id,
        rights_status=rights_status,
        audio_refs=audio_refs,
    )


class FakeVideoRenderer:
    """Deterministic fake — does not claim real codec encoding."""

    provider_id = "fake-video-render"

    def render(self, *, recipe: VideoRecipe) -> dict:
        if recipe.rights_status == RIGHTS_THIRD_PARTY_RESTRICTED:
            raise MediaError(MEDIA_RIGHTS_DENIED, "restricted_rights")
        if recipe.duration_sec <= 0:
            raise MediaError(MEDIA_VIDEO_RENDER_FAILED, "invalid_duration")
        # Placeholder MP4-ish identity bytes (not a real container decode path)
        payload = f"FAKE_MP4|{recipe.recipe_id}|{recipe.aspect_ratio}|{recipe.duration_sec}".encode()
        digest = hashlib.sha256(payload).hexdigest()
        return {
            "data": payload,
            "mime_type": "video/mp4",
            "provider_id": self.provider_id,
            "content_hash": digest,
            "width": 1080 if "9:16" in recipe.aspect_ratio else 1280,
            "height": 1920 if "9:16" in recipe.aspect_ratio else 720,
            "fake": True,
            "rights_status": recipe.rights_status if recipe.rights_status != RIGHTS_UNKNOWN else RIGHTS_OWNED,
        }
