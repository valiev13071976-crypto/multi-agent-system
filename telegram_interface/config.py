"""Telegram interface configuration — env names only, no secret values.

Live config semantics (Block 22B Phase 1A):

TELEGRAM_INTERFACE_ENABLED
  Mount/build the canonical telegram_interface runtime (default true).
  false → webhook routes remain in OpenAPI but return 503.

TELEGRAM_LIVE_ACTIVE
  Human LIVE AUTHORIZATION for this process. Does NOT reject inbound updates.
  false → live Telegram network is forbidden (Fake provider only).

TELEGRAM_ENABLED
  Select ProductionTelegramProvider (existing factory flag).
  Live network is used only when BOTH TELEGRAM_LIVE_ACTIVE and TELEGRAM_ENABLED
  are true. Missing either flag → Fake. Both true + missing token → fail closed.
  Both true → never silently fall back to Fake.

TELEGRAM_LIVE_ACTIVE=true does not mean "drop every update".
Unapproved live network (LIVE not selected) is the LIVE FORBIDDEN safety gate
for outbound Bot API use, not for webhook ingest.
"""

from __future__ import annotations

import os
from pathlib import Path


def _truthy(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def telegram_interface_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = source.get("TELEGRAM_INTERFACE_ENABLED")
    if raw is None or str(raw).strip() == "":
        return True
    return _truthy(raw)


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


def telegram_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return _truthy(source.get("TELEGRAM_ENABLED"))


def telegram_live_active(env: dict | None = None) -> bool:
    """LIVE AUTHORIZATION flag — does not by itself reject inbound updates."""
    source = env if env is not None else os.environ
    return _truthy(source.get("TELEGRAM_LIVE_ACTIVE"))


def telegram_live_network_selected(env: dict | None = None) -> bool:
    """True only when live network is explicitly approved AND enabled."""
    source = env if env is not None else os.environ
    return telegram_live_active(source) and telegram_enabled(source)


def telegram_token_configured(env: dict | None = None) -> bool:
    """Presence only — never return or log the value."""
    source = env if env is not None else os.environ
    raw = source.get("TELEGRAM_BOT_TOKEN")
    return raw is not None and bool(str(raw).strip())


def require_durable_telegram_db(env: dict | None, db_path: str) -> None:
    """LIVE network selection may not use ephemeral ./telegram_interface.sqlite."""
    source = env if env is not None else os.environ
    if not telegram_live_network_selected(source):
        return
    data_dir = str(source.get("PANDA_DATA_DIR") or "").strip()
    if not data_dir:
        raise RuntimeError("PANDA_DATA_DIR required when Telegram live network is selected")
    root = Path(data_dir).resolve()
    resolved = Path(db_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_INTERFACE_DB_PATH must be under PANDA_DATA_DIR for live Telegram") from exc


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
            "REQUIRED": "REQUIRED with TELEGRAM_LIVE_ACTIVE for live provider",
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
            "REQUIRED": "OPTIONAL until human-approved live (authorization, not ingest deny)",
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
    if telegram_live_network_selected(source) and not telegram_webhook_secret(source):
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET required when Telegram live network is selected")
