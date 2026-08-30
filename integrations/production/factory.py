"""Build production integration adapters from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from integrations.production.adapters.ai import ai_provider_metadata
from integrations.production.adapters.billing import build_billing_provider
from integrations.production.adapters.commerce import build_commerce_adapters
from integrations.production.adapters.email import build_email_provider
from integrations.production.adapters.media import build_image_provider
from integrations.production.adapters.seo import build_seo_providers
from integrations.production.adapters.speech import build_speech_providers
from integrations.production.adapters.telegram import build_telegram_provider
from integrations.production.config import ProductionIntegrationConfig
from integrations.production.credentials import inventory_as_dict
from integrations.production.metadata import (
    VERIFICATION_CODE,
    VERIFICATION_CONFIG,
    VERIFICATION_NOT_ENABLED,
    VERIFICATION_OPERATOR,
    ProviderMetadata,
)
from integrations.production.observability import ProviderObservability
from integrations.production.registry import ProductionProviderRegistry
from production_foundation.config import reject_placeholder_secret


def _configured(env: dict, *keys: str) -> bool:
    return any(str(env.get(k) or "").strip() and reject_placeholder_secret(str(env.get(k) or "")) for k in keys)


@dataclass
class ProductionIntegrationBundle:
    config: ProductionIntegrationConfig
    registry: ProductionProviderRegistry
    obs: ProviderObservability
    billing_provider: Any
    telegram_provider: Any
    email_provider: Any
    stt_provider: Any
    tts_provider: Any
    search_console_provider: Any
    analytics_provider: Any
    performance_provider: Any
    image_provider: Any
    commerce_adapters: dict[str, Any] = field(default_factory=dict)
    credential_inventory: list[dict] = field(default_factory=list)

    def close(self) -> None:
        for obj in (
            self.billing_provider,
            self.telegram_provider,
            self.email_provider,
            self.stt_provider,
            self.tts_provider,
            self.image_provider,
            *self.commerce_adapters.values(),
        ):
            if hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass


def build_production_integrations(
    *,
    env: dict | None = None,
    integration_service: Any | None = None,
    health_tracker=None,
) -> ProductionIntegrationBundle:
    source = env if env is not None else dict(os.environ)
    cfg = ProductionIntegrationConfig.from_env(source)
    obs = ProviderObservability()
    registry = ProductionProviderRegistry()

    billing = build_billing_provider(source)
    registry.register(
        ProviderMetadata(
            provider_id=billing.name,
            provider_type="billing",
            enabled=cfg.billing_provider not in {"blocked_fake"},
            configured=_configured(source, "STRIPE_SECRET_KEY", "SAAS_BILLING_WEBHOOK_SECRET") or billing.name == "fake",
            verification_status=VERIFICATION_CODE if billing.name == "fake" else (VERIFICATION_CONFIG if _configured(source, "STRIPE_SECRET_KEY") else VERIFICATION_OPERATOR),
            capabilities=("checkout", "webhook"),
            timeout_seconds=30.0,
            credential_ref="STRIPE_SECRET_KEY",
            production_mode=cfg.production,
            webhook=True,
            tenant_scope="global",
        ),
        billing,
    )

    telegram = build_telegram_provider(source)
    tg_configured = _configured(source, "TELEGRAM_BOT_TOKEN")
    registry.register(
        ProviderMetadata(
            provider_id="telegram",
            provider_type="telegram",
            enabled=cfg.telegram_enabled or not cfg.production,
            configured=tg_configured,
            verification_status=VERIFICATION_CONFIG if tg_configured else (VERIFICATION_CODE if not cfg.telegram_enabled else VERIFICATION_OPERATOR),
            capabilities=("send_message", "webhook"),
            timeout_seconds=15.0,
            credential_ref="TELEGRAM_BOT_TOKEN",
            production_mode=cfg.production,
            webhook=True,
        ),
        telegram,
    )

    email = build_email_provider(source)
    email_configured = _configured(source, "EMAIL_API_KEY", "EMAIL_FROM_ADDRESS")
    registry.register(
        ProviderMetadata(
            provider_id=getattr(email, "provider_id", "email"),
            provider_type="email",
            enabled=cfg.email_enabled or not cfg.production,
            configured=email_configured,
            verification_status=VERIFICATION_CONFIG if email_configured else VERIFICATION_CODE,
            capabilities=("transactional",),
            timeout_seconds=20.0,
            credential_ref="EMAIL_API_KEY",
            production_mode=cfg.production,
        ),
        email,
    )

    stt, tts = build_speech_providers(source)
    speech_configured = _configured(source, "SPEECH_API_KEY", "OPENAI_API_KEY")
    speech_status = VERIFICATION_CONFIG if speech_configured else VERIFICATION_CODE
    registry.register(
        ProviderMetadata(provider_id="speech_stt", provider_type="speech", enabled=True, configured=speech_configured, verification_status=speech_status, capabilities=("stt",), credential_ref="SPEECH_API_KEY"),
        stt,
    )
    registry.register(
        ProviderMetadata(provider_id="speech_tts", provider_type="speech", enabled=True, configured=speech_configured, verification_status=speech_status, capabilities=("tts",), credential_ref="SPEECH_API_KEY"),
        tts,
    )

    gsc, analytics, performance = build_seo_providers(source)
    seo_configured = _configured(source, "GOOGLE_SERVICE_ACCOUNT_JSON")
    seo_enabled = cfg.seo_enabled
    registry.register(
        ProviderMetadata(
            provider_id="google_search_console",
            provider_type="seo",
            enabled=seo_enabled or not cfg.production,
            configured=seo_configured,
            verification_status=VERIFICATION_CONFIG if seo_configured else (VERIFICATION_CODE if not seo_enabled else VERIFICATION_OPERATOR),
            capabilities=("search_console.read",),
            credential_ref="GOOGLE_SERVICE_ACCOUNT_JSON",
            tenant_scope="tenant",
        ),
        gsc,
    )
    registry.register(
        ProviderMetadata(
            provider_id="google_analytics",
            provider_type="seo",
            enabled=seo_enabled or not cfg.production,
            configured=seo_configured,
            verification_status=VERIFICATION_CONFIG if seo_configured else (VERIFICATION_CODE if not seo_enabled else VERIFICATION_OPERATOR),
            capabilities=("analytics.read",),
            credential_ref="GA4_PROPERTY_ID",
            tenant_scope="tenant",
        ),
        analytics,
    )
    registry.register(
        ProviderMetadata(provider_id="pagespeed", provider_type="seo", enabled=True, configured=True, verification_status=VERIFICATION_CODE, capabilities=("performance.read",)),
        performance,
    )

    image = build_image_provider(source)
    image_configured = _configured(source, "MEDIA_IMAGE_API_KEY", "OPENAI_API_KEY")
    registry.register(
        ProviderMetadata(
            provider_id=getattr(image, "provider_id", "media_image"),
            provider_type="media",
            enabled=True,
            configured=image_configured or getattr(image, "provider_id", "").startswith("fake"),
            verification_status=VERIFICATION_CONFIG if image_configured else VERIFICATION_CODE,
            capabilities=("image.generate",),
            credential_ref="MEDIA_IMAGE_API_KEY",
        ),
        image,
    )

    video_enabled = cfg.media_video_enabled
    registry.register(
        ProviderMetadata(
            provider_id="media_video",
            provider_type="media",
            enabled=video_enabled,
            configured=False,
            verification_status=VERIFICATION_NOT_ENABLED if not video_enabled else VERIFICATION_OPERATOR,
            capabilities=("video.generate",),
        ),
    )

    commerce = build_commerce_adapters(source, integration_service)
    for pid, adapter in commerce.items():
        registry.register(
            ProviderMetadata(
                provider_id=pid,
                provider_type="commerce",
                enabled=True,
                configured=adapter.__class__.__name__ == "ProductionCommerceAdapter",
                verification_status=VERIFICATION_CODE,
                capabilities=("read", "write"),
                tenant_scope="tenant",
            ),
            adapter,
        )

    for meta in ai_provider_metadata(source, health_tracker=health_tracker):
        registry.register(meta)

    return ProductionIntegrationBundle(
        config=cfg,
        registry=registry,
        obs=obs,
        billing_provider=billing,
        telegram_provider=telegram,
        email_provider=email,
        stt_provider=stt,
        tts_provider=tts,
        search_console_provider=gsc,
        analytics_provider=analytics,
        performance_provider=performance,
        image_provider=image,
        commerce_adapters=commerce,
        credential_inventory=inventory_as_dict(source),
    )
