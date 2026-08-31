"""Versioned target media profiles — configurable, not invented official rules."""

from __future__ import annotations

from product_media.errors import MEDIA_TARGET_PROFILE_INVALID, MediaError
from product_media.platform_models import TARGET_PROFILE_VERSION, TargetMediaProfile

# Profiles are configurable fixtures. source_of_rules="configurable" —
# not claimed as live marketplace API requirements.
_BUILTIN: dict[str, TargetMediaProfile] = {
    "website_hero": TargetMediaProfile(
        profile_id="website_hero",
        channel="website",
        asset_role="hero",
        width=1200,
        height=1200,
        format="webp",
        allow_alpha=True,
        background="transparent",
        source_of_rules="configurable",
    ),
    "website_gallery": TargetMediaProfile(
        profile_id="website_gallery",
        channel="website",
        asset_role="gallery",
        width=800,
        height=800,
        format="jpeg",
        allow_alpha=False,
        source_of_rules="configurable",
    ),
    "website_thumbnail": TargetMediaProfile(
        profile_id="website_thumbnail",
        channel="website",
        asset_role="thumbnail",
        width=256,
        height=256,
        format="jpeg",
        allow_alpha=False,
        source_of_rules="configurable",
    ),
    "marketplace_main": TargetMediaProfile(
        profile_id="marketplace_main",
        channel="marketplace",
        asset_role="main",
        width=1000,
        height=1000,
        format="jpeg",
        allow_alpha=False,
        background="white",
        source_of_rules="configurable",
    ),
    "wb_main": TargetMediaProfile(
        profile_id="wb_main",
        channel="wildberries",
        asset_role="main",
        width=900,
        height=1200,
        format="jpeg",
        allow_alpha=False,
        background="white",
        source_of_rules="configurable",
        version=TARGET_PROFILE_VERSION,
    ),
    "ozon_main": TargetMediaProfile(
        profile_id="ozon_main",
        channel="ozon",
        asset_role="main",
        width=1200,
        height=1200,
        format="jpeg",
        allow_alpha=False,
        background="white",
        source_of_rules="configurable",
    ),
    "yandex_market_main": TargetMediaProfile(
        profile_id="yandex_market_main",
        channel="yandex_market",
        asset_role="main",
        width=1000,
        height=1000,
        format="jpeg",
        allow_alpha=False,
        background="white",
        source_of_rules="configurable",
    ),
    "social_square": TargetMediaProfile(
        profile_id="social_square",
        channel="social",
        asset_role="square",
        width=1080,
        height=1080,
        format="jpeg",
        allow_alpha=False,
        source_of_rules="configurable",
    ),
    "social_portrait": TargetMediaProfile(
        profile_id="social_portrait",
        channel="social",
        asset_role="portrait",
        width=1080,
        height=1350,
        format="jpeg",
        allow_alpha=False,
        source_of_rules="configurable",
    ),
    "social_story": TargetMediaProfile(
        profile_id="social_story",
        channel="social",
        asset_role="story",
        width=1080,
        height=1920,
        format="jpeg",
        allow_alpha=False,
        source_of_rules="configurable",
        safe_margin_pct=0.1,
    ),
    "banner_landscape": TargetMediaProfile(
        profile_id="banner_landscape",
        channel="banner",
        asset_role="banner",
        width=1200,
        height=628,
        format="jpeg",
        allow_alpha=False,
        source_of_rules="configurable",
    ),
    "video_short_9x16": TargetMediaProfile(
        profile_id="video_short_9x16",
        channel="video",
        asset_role="short",
        width=1080,
        height=1920,
        format="mp4",
        allow_alpha=False,
        source_of_rules="configurable",
    ),
}

# Legacy quality profile name → target profile id
LEGACY_QUALITY_MAP = {
    "website": "website_hero",
    "marketplace": "marketplace_main",
    "social": "social_square",
}


def get_target_profile(profile_id: str) -> TargetMediaProfile:
    pid = LEGACY_QUALITY_MAP.get(profile_id, profile_id)
    profile = _BUILTIN.get(pid)
    if profile is None:
        raise MediaError(MEDIA_TARGET_PROFILE_INVALID, profile_id)
    return profile


def list_marketplace_profiles() -> tuple[TargetMediaProfile, ...]:
    return (
        get_target_profile("wb_main"),
        get_target_profile("ozon_main"),
        get_target_profile("yandex_market_main"),
    )


def register_target_profile(profile: TargetMediaProfile) -> None:
    """Allow tenant/config overrides of marketplace rules without code forks."""
    _BUILTIN[profile.profile_id] = profile
