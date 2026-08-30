"""Secret inventory and redaction helpers."""

from __future__ import annotations

from dataclasses import dataclass

SECRET_CLASSES = (
    ("AI provider keys", ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"), "OPTIONAL"),
    ("Auth API keys", ("PANDA_API_KEYS",), "REQUIRED_PROD"),
    ("Session/capability signing", ("PANDA_CAPABILITY_SIGNING_KEY", "SECURITY_SESSION_SECRET"), "REQUIRED_PROD"),
    ("Billing webhook", ("SAAS_BILLING_WEBHOOK_SECRET", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"), "OPTIONAL"),
    ("Telegram", ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"), "OPTIONAL"),
    ("Transactional email", ("EMAIL_API_KEY", "EMAIL_FROM_ADDRESS"), "OPTIONAL"),
    ("Speech", ("SPEECH_API_KEY",), "OPTIONAL"),
    ("SEO / Analytics", ("GOOGLE_SERVICE_ACCOUNT_JSON", "GSC_PROPERTY_ID", "GA4_PROPERTY_ID"), "OPTIONAL"),
    ("Media generation", ("MEDIA_IMAGE_API_KEY",), "OPTIONAL"),
    ("Commerce integrations", ("BITRIX_WEBHOOK_URL", "ONEC_API_URL", "ONEC_API_TOKEN"), "OPTIONAL"),
    ("Encryption key", ("PANDA_ENCRYPTION_KEY",), "OPTIONAL"),
    ("Alert webhook", ("PANDA_ALERT_WEBHOOK_URL",), "OPTIONAL"),
    ("GitHub token", ("GITHUB_WRITE_TOKEN",), "OPTIONAL"),
    ("Search API", ("SEARCH_API_KEY",), "OPTIONAL"),
)


@dataclass(frozen=True)
class SecretInventoryEntry:
    category: str
    env_keys: tuple[str, ...]
    classification: str


def secret_inventory() -> tuple[SecretInventoryEntry, ...]:
    return tuple(SecretInventoryEntry(c, tuple(k), cls) for c, k, cls in SECRET_CLASSES)


def inventory_status(env: dict) -> list[dict]:
    from production_foundation.config import is_production, reject_placeholder_secret

    prod = is_production(env)
    out = []
    for entry in secret_inventory():
        present = any(str(env.get(k) or "").strip() for k in entry.env_keys)
        placeholder = any(not reject_placeholder_secret(str(env.get(k) or "")) for k in entry.env_keys if env.get(k))
        status = "configured" if present and not placeholder else "missing"
        if prod and entry.classification == "REQUIRED_PROD" and status == "missing":
            status = "required_missing"
        out.append(
            {
                "category": entry.category,
                "env_keys": list(entry.env_keys),
                "classification": entry.classification,
                "status": status,
            }
        )
    return out
