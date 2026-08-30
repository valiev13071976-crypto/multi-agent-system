"""Stage-2 credential inventory — metadata only, never raw values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from production_foundation.config import reject_placeholder_secret


@dataclass(frozen=True)
class CredentialRecord:
    provider: str
    credential_type: str
    scope: str
    environment: str
    required: bool
    configured: bool
    rotation_supported: bool
    last_validation_status: str
    env_keys: tuple[str, ...]


STAGE2_CREDENTIALS: tuple[tuple[str, str, str, str, bool, tuple[str, ...]], ...] = (
    ("openai", "api_key", "global", "ai", False, ("OPENAI_API_KEY",)),
    ("anthropic", "api_key", "global", "ai", False, ("ANTHROPIC_API_KEY",)),
    ("gemini", "api_key", "global", "ai", False, ("GEMINI_API_KEY",)),
    ("grok", "api_key", "global", "ai", False, ("XAI_API_KEY",)),
    ("deepseek", "api_key", "global", "ai", False, ("DEEPSEEK_API_KEY",)),
    ("telegram", "bot_token", "global", "telegram", False, ("TELEGRAM_BOT_TOKEN",)),
    ("telegram", "webhook_secret", "global", "telegram", False, ("TELEGRAM_WEBHOOK_SECRET",)),
    ("email", "api_key", "global", "email", False, ("EMAIL_API_KEY",)),
    ("billing", "secret_key", "global", "billing", False, ("STRIPE_SECRET_KEY", "SAAS_BILLING_WEBHOOK_SECRET")),
    ("speech", "api_key", "global", "speech", False, ("OPENAI_API_KEY", "SPEECH_API_KEY")),
    ("bitrix", "webhook_url", "tenant", "commerce", False, ("BITRIX_WEBHOOK_URL",)),
    ("onec", "credentials", "tenant", "commerce", False, ("ONEC_API_URL", "ONEC_API_TOKEN")),
    ("google_seo", "service_account", "global", "seo", False, ("GOOGLE_SERVICE_ACCOUNT_JSON",)),
    ("google_analytics", "service_account", "global", "seo", False, ("GA4_PROPERTY_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")),
    ("media_image", "api_key", "global", "media", False, ("OPENAI_API_KEY", "MEDIA_IMAGE_API_KEY")),
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def credential_inventory(env: dict) -> list[CredentialRecord]:
    prod = str(env.get("PANDA_ENV") or env.get("ENVIRONMENT") or "").strip().lower() in {"production", "prod"}
    out: list[CredentialRecord] = []
    for provider, ctype, scope, category, required, keys in STAGE2_CREDENTIALS:
        present = any(str(env.get(k) or "").strip() for k in keys)
        valid = present and all(
            not env.get(k) or reject_placeholder_secret(str(env.get(k) or "")) for k in keys if env.get(k)
        )
        configured = present and valid
        if prod and required and not configured:
            status = "required_missing"
        elif configured:
            status = "configured"
        else:
            status = "missing"
        out.append(
            CredentialRecord(
                provider=provider,
                credential_type=ctype,
                scope=scope,
                environment=category,
                required=required,
                configured=configured,
                rotation_supported=True,
                last_validation_status=status,
                env_keys=keys,
            )
        )
    return out


def inventory_as_dict(env: dict) -> list[dict]:
    return [
        {
            "provider": r.provider,
            "credential_type": r.credential_type,
            "scope": r.scope,
            "environment": r.environment,
            "required": r.required,
            "configured": r.configured,
            "rotation_supported": r.rotation_supported,
            "last_validation_status": r.last_validation_status,
            "env_keys": list(r.env_keys),
            "checked_at": _utc(),
        }
        for r in credential_inventory(env)
    ]
