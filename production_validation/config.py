"""Stage-3 validation configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LaunchProviderSpec:
    provider_id: str
    requirement: str  # REQUIRED_FOR_STAGE4 | OPTIONAL | NOT_USED_AT_LAUNCH
    env_keys: tuple[str, ...] = ()


LAUNCH_PROVIDER_MATRIX: tuple[LaunchProviderSpec, ...] = (
    LaunchProviderSpec("openai", "REQUIRED_FOR_STAGE4", ("OPENAI_API_KEY",)),
    LaunchProviderSpec("anthropic", "OPTIONAL", ("ANTHROPIC_API_KEY",)),
    LaunchProviderSpec("gemini", "OPTIONAL", ("GEMINI_API_KEY",)),
    LaunchProviderSpec("telegram", "OPTIONAL", ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ENABLED")),
    LaunchProviderSpec("email", "OPTIONAL", ("EMAIL_ENABLED", "EMAIL_API_KEY")),
    LaunchProviderSpec("stripe", "OPTIONAL", ("SAAS_BILLING_PROVIDER", "STRIPE_SECRET_KEY")),
    LaunchProviderSpec("speech", "OPTIONAL", ("SPEECH_PROVIDER", "SPEECH_API_KEY")),
    LaunchProviderSpec("google_search_console", "NOT_USED_AT_LAUNCH", ("SEO_PRODUCTION_ENABLED",)),
    LaunchProviderSpec("media_image", "OPTIONAL", ("MEDIA_IMAGE_PROVIDER",)),
    LaunchProviderSpec("media_video", "NOT_USED_AT_LAUNCH", ("MEDIA_VIDEO_ENABLED",)),
    LaunchProviderSpec("bitrix", "NOT_USED_AT_LAUNCH", ("BITRIX_ENABLED",)),
    LaunchProviderSpec("onec", "NOT_USED_AT_LAUNCH", ("ONEC_API_URL",)),
    LaunchProviderSpec("crm", "NOT_USED_AT_LAUNCH", ()),
)


@dataclass
class ValidationConfig:
    production_url: str
    release_identity: str
    environment: str
    max_load_concurrency: int = 20
    max_load_duration_seconds: float = 30.0
    max_load_requests: int = 500
    soak_duration_seconds: float = 15.0
    allowed_hosts: tuple[str, ...] = ()
    live_enabled: bool = False

    @classmethod
    def from_env(cls, env: dict | None = None) -> ValidationConfig:
        source = env if env is not None else os.environ
        url = str(source.get("PRODUCTION_VALIDATION_URL") or source.get("PUBLIC_URL") or "").strip().rstrip("/")
        env_name = str(source.get("PANDA_ENV") or source.get("ENVIRONMENT") or "development").strip().lower()
        release = str(source.get("RELEASE_IDENTITY") or source.get("GIT_COMMIT") or source.get("RAILWAY_GIT_COMMIT_SHA") or "UNKNOWN").strip()
        allowed = tuple(h.strip() for h in str(source.get("PRODUCTION_VALIDATION_ALLOWED_HOSTS") or "").split(",") if h.strip())
        if url and allowed and not any(url.startswith(f"https://{h}") or url.startswith(f"http://{h}") for h in allowed):
            url = ""
        return cls(
            production_url=url,
            release_identity=release,
            environment=env_name,
            max_load_concurrency=int(source.get("STAGE3_LOAD_MAX_CONCURRENCY") or 20),
            max_load_duration_seconds=float(source.get("STAGE3_LOAD_MAX_DURATION") or 30),
            max_load_requests=int(source.get("STAGE3_LOAD_MAX_REQUESTS") or 500),
            soak_duration_seconds=float(source.get("STAGE3_SOAK_DURATION") or 15),
            allowed_hosts=allowed or (tuple([url.split("://", 1)[-1].split("/")[0]]) if url else ()),
            live_enabled=bool(url) and env_name in {"production", "prod", "staging"},
        )
