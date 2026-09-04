"""Telegram interface configuration — env names only, no secret values."""

from __future__ import annotations

import os


def telegram_interface_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get("TELEGRAM_INTERFACE_ENABLED") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def telegram_webhook_secret(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    return str(source.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()


def telegram_bot_token(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    return str(source.get("TELEGRAM_BOT_TOKEN") or "").strip()


def telegram_interface_db_path(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    return str(
        source.get("TELEGRAM_INTERFACE_DB_PATH")
        or os.path.join(source.get("PANDA_DATA_DIR") or ".", "telegram_interface.sqlite")
    )


def telegram_live_active(env: dict | None = None) -> bool:
    """Live Telegram network is opt-in. Block 22A keeps this false."""
    source = env if env is not None else os.environ
    return str(source.get("TELEGRAM_LIVE_ACTIVE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def telegram_secret_contract() -> list[dict[str, str]]:
    """Variable NAMES and contract status only — never secret values."""
    return [
        {
            "VARIABLE_NAME": "TELEGRAM_BOT_TOKEN",
            "REQUIRED": "REQUIRED for live activation only",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_WEBHOOK_SECRET",
            "REQUIRED": "REQUIRED in production when interface enabled",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_INTERFACE_ENABLED",
            "REQUIRED": "OPTIONAL",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_ENABLED",
            "REQUIRED": "OPTIONAL (false = FakeTelegramProvider)",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_INTERFACE_DB_PATH",
            "REQUIRED": "OPTIONAL",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_DEFAULT_TENANT",
            "REQUIRED": "OPTIONAL",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_LIVE_ACTIVE",
            "REQUIRED": "OPTIONAL (must remain false until human approval)",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_INTERFACE_ENGINEERING_READY",
            "REQUIRED": "OPTIONAL documentation flag",
            "STATUS": "CONFIGURED-CONTRACT",
        },
    ]


def require_webhook_secret_in_production(env: dict | None = None) -> None:
    source = env if env is not None else os.environ
    prod = str(source.get("PANDA_ENV") or source.get("ENVIRONMENT") or "").strip().lower() in {
        "production",
        "prod",
    }
    if prod and telegram_interface_enabled(source) and not telegram_webhook_secret(source):
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET required in production when TELEGRAM_INTERFACE_ENABLED")
