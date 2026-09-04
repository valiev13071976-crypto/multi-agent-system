"""Block 22A Telegram pre-activation readiness — names and flags only."""

from __future__ import annotations

from telegram_interface.config import telegram_secret_contract

TELEGRAM_LIVE_ACTIVE = False
TELEGRAM_LIVE_VERIFIED = False
REAL_MESSAGES_SENT = 0
REAL_UPDATES_RECEIVED = 0

CANONICAL_MODE = "webhook"
POLLING_STATUS = "NOT_APPLICABLE"

READINESS = "READY_FOR_HUMAN_APPROVAL"


def live_activation_state() -> dict:
    return {
        "telegram_live_active": TELEGRAM_LIVE_ACTIVE,
        "telegram_live_verified": TELEGRAM_LIVE_VERIFIED,
        "real_messages_sent": REAL_MESSAGES_SENT,
        "real_updates_received": REAL_UPDATES_RECEIVED,
    }


def preactivation_readiness() -> dict:
    return {
        "block": "22A",
        "readiness": READINESS,
        "canonical_flow": (
            "Telegram update fixture → validate → bind → Business Assistant API "
            "→ Panda AI Core / governed tools → Telegram fixture adapter"
        ),
        "second_ai_core": False,
        "inbound_mode": CANONICAL_MODE,
        "polling": POLLING_STATUS,
        "secret_contract": telegram_secret_contract(),
        "live_activation": live_activation_state(),
        "activation_runbook": "docs/telegram-pre-activation-runbook.md",
    }
