"""Production integration configuration from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _mode(env: dict) -> str:
    return str(env.get("PANDA_ENV") or env.get("ENVIRONMENT") or "development").strip().lower()


def is_production_env(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return _mode(source) in {"production", "prod"}


@dataclass(frozen=True)
class ProductionIntegrationConfig:
    ai_enabled: bool
    telegram_enabled: bool
    email_enabled: bool
    billing_provider: str
    billing_mode: str
    speech_provider: str
    commerce_enabled: bool
    seo_enabled: bool
    media_image_provider: str
    media_video_enabled: bool
    production: bool

    @classmethod
    def from_env(cls, env: dict | None = None) -> ProductionIntegrationConfig:
        source = env if env is not None else os.environ
        prod = is_production_env(source)
        billing_provider = str(source.get("SAAS_BILLING_PROVIDER") or "fake").strip().lower()
        if prod and billing_provider == "fake" and _bool(source.get("SAAS_BILLING_ENABLED"), True):
            billing_provider = "blocked_fake"
        return cls(
            ai_enabled=_bool(source.get("PRODUCTION_AI_ENABLED"), True),
            telegram_enabled=_bool(source.get("TELEGRAM_ENABLED"), False),
            email_enabled=_bool(source.get("EMAIL_ENABLED"), False),
            billing_provider=billing_provider,
            billing_mode=str(source.get("SAAS_BILLING_MODE") or "test").strip().lower(),
            speech_provider=str(source.get("SPEECH_PROVIDER") or "fake").strip().lower(),
            commerce_enabled=_bool(source.get("COMMERCE_INTEGRATIONS_ENABLED"), True),
            seo_enabled=_bool(source.get("SEO_PRODUCTION_ENABLED"), False),
            media_image_provider=str(source.get("MEDIA_IMAGE_PROVIDER") or "fake").strip().lower(),
            media_video_enabled=_bool(source.get("MEDIA_VIDEO_ENABLED"), False),
            production=prod,
        )

    def assert_no_fake_in_production(self) -> None:
        if not self.production:
            return
        if self.billing_provider in {"fake", "blocked_fake"}:
            raise RuntimeError("production_billing_fake_forbidden")
        if self.speech_provider == "fake" and _bool(os.environ.get("UI_CHAT_VOICE_ENABLED")):
            raise RuntimeError("production_speech_fake_forbidden")
