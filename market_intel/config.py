"""Market Intelligence configuration — env NAMES only, never values.

Mirrors the live-gate semantics the Bot API interface already uses
(``telegram_interface.config``) so both Telegram surfaces are activated
the same way, but with SEPARATE flags: a bot token and a user-account
session are different credentials with very different blast radius.

MARKET_INTEL_ENABLED
  Build/mount the Market Intelligence runtime. Default FALSE -- this is a
  new subsystem and must not turn itself on in an existing deployment.

TELEGRAM_USER_CLIENT_ENABLED
  Select the real MTProto user client instead of the offline fixture one.

TELEGRAM_USER_LIVE_ACTIVE
  Human LIVE AUTHORIZATION for reading with the owner's OWN account.
  The real client is used only when BOTH flags are true; either one
  missing -> fixture client. Both true + missing api id/hash -> fail
  closed, never a silent fallback to fixtures.

A user-account session authenticates AS THE OWNER and can read
everything the owner can, so it is stored encrypted at rest
(``market_intel.session_vault``) and requires PANDA_ENCRYPTION_KEY.
"""

from __future__ import annotations

import os
from pathlib import Path


def _truthy(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def market_intel_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return _truthy(source.get("MARKET_INTEL_ENABLED"))


def telegram_user_client_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return _truthy(source.get("TELEGRAM_USER_CLIENT_ENABLED"))


def telegram_user_live_active(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return _truthy(source.get("TELEGRAM_USER_LIVE_ACTIVE"))


def telegram_user_live_selected(env: dict | None = None) -> bool:
    """True only when reading with the owner's account is explicitly
    approved AND enabled."""
    source = env if env is not None else os.environ
    return telegram_user_live_active(source) and telegram_user_client_enabled(source)


def telegram_api_credentials_configured(env: dict | None = None) -> bool:
    """Presence only — never return or log the values."""
    source = env if env is not None else os.environ
    return bool(str(source.get("TELEGRAM_API_ID") or "").strip()) and bool(
        str(source.get("TELEGRAM_API_HASH") or "").strip()
    )


def market_intel_db_path(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    return str(
        source.get("MARKET_INTEL_DB_PATH")
        or os.path.join(str(source.get("PANDA_DATA_DIR") or "."), "market_intel.sqlite")
    )


def market_intel_default_tenant(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    return str(source.get("MARKET_INTEL_DEFAULT_TENANT") or "tenant-a")


def require_durable_market_intel_db(env: dict | None, db_path: str) -> None:
    """Live account reading may not persist observations to an ephemeral
    ./market_intel.sqlite — same rule the Bot API interface applies to its
    own store."""
    source = env if env is not None else os.environ
    if not telegram_user_live_selected(source):
        return
    data_dir = str(source.get("PANDA_DATA_DIR") or "").strip()
    if not data_dir:
        raise RuntimeError("PANDA_DATA_DIR required when Telegram user client is live")
    try:
        Path(db_path).resolve().relative_to(Path(data_dir).resolve())
    except ValueError as exc:
        raise RuntimeError("MARKET_INTEL_DB_PATH must be under PANDA_DATA_DIR for live reading") from exc


def market_intel_secret_contract() -> list[dict[str, str]]:
    """Variable NAMES and contract status only — never secret values."""
    return [
        {
            "VARIABLE_NAME": "MARKET_INTEL_ENABLED",
            "REQUIRED": "OPTIONAL (default false)",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_USER_CLIENT_ENABLED",
            "REQUIRED": "REQUIRED with TELEGRAM_USER_LIVE_ACTIVE for the real MTProto client",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_USER_LIVE_ACTIVE",
            "REQUIRED": "OPTIONAL until human-approved live account reading",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_API_ID",
            "REQUIRED": "REQUIRED for live account reading only",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "TELEGRAM_API_HASH",
            "REQUIRED": "REQUIRED for live account reading only",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "PANDA_ENCRYPTION_KEY",
            "REQUIRED": "REQUIRED to store a user session at rest",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "MARKET_INTEL_DB_PATH",
            "REQUIRED": "OPTIONAL",
            "STATUS": "CONFIGURED-CONTRACT",
        },
        {
            "VARIABLE_NAME": "MARKET_INTEL_DEFAULT_TENANT",
            "REQUIRED": "OPTIONAL",
            "STATUS": "CONFIGURED-CONTRACT",
        },
    ]
