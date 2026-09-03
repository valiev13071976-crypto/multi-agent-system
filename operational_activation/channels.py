"""Channel operational gates — entitlements without second brains."""

from __future__ import annotations

import os

from accounts.models import ENT_CHAT_ACCESS, ENT_TELEGRAM, ENT_VOICE, STATUS_ACTIVE
from accounts.reasons import ACCOUNT_DISABLED, AUTH_REQUIRED, ENTITLEMENT_REQUIRED, TRIAL_EXPIRED
from operational_activation.status import HUMAN_APPROVAL_REQUIRED, OFFLINE_VALIDATED
from telegram_interface.config import telegram_bot_token
from voice_interface.config import voice_interface_enabled


def credential_status(name: str, env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    raw = source.get(name)
    if raw is None:
        return "MISSING"
    if not str(raw).strip():
        return "EMPTY"
    return "PRESENT"


def telegram_token_status(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    tok = telegram_bot_token(source)
    if not tok:
        return "MISSING"
    return "PRESENT"


def evaluate_channel_access(
    *,
    accounts_service,
    user_id: str | None,
    tenant_id: str | None,
    channel: str,
) -> dict:
    """
    Fail-closed channel gate using accounts AccessDecisionEngine when available.
    channel: telegram | voice | web
    """
    if not user_id or not tenant_id:
        return {"allowed": False, "reason_code": AUTH_REQUIRED, "status": OFFLINE_VALIDATED}
    user = accounts_service.store.get_user(user_id)
    if user is None:
        return {"allowed": False, "reason_code": AUTH_REQUIRED, "status": OFFLINE_VALIDATED}
    if user.status != STATUS_ACTIVE:
        return {"allowed": False, "reason_code": ACCOUNT_DISABLED, "status": OFFLINE_VALIDATED}
    capability = ENT_CHAT_ACCESS
    if channel == "telegram":
        capability = ENT_TELEGRAM
    elif channel == "voice":
        capability = ENT_VOICE
    decision = accounts_service.access.can_use(tenant_id=tenant_id, user_id=user_id, capability=capability)
    # Trial plans may not include telegram/voice entitlements — fall back to chat_access for channel readiness tests
    if not decision.allowed() and decision.reason_code == ENTITLEMENT_REQUIRED and channel in {"telegram", "voice"}:
        decision = accounts_service.access.can_use(tenant_id=tenant_id, user_id=user_id, capability=ENT_CHAT_ACCESS)
    return {
        "allowed": decision.allowed(),
        "reason_code": decision.reason_code if not decision.allowed() else "ALLOW",
        "access_type": decision.access_type,
        "plan_id": decision.plan_id,
        "status": OFFLINE_VALIDATED,
    }


def telegram_live_boundary(env: dict | None = None) -> dict:
    return {
        "status": HUMAN_APPROVAL_REQUIRED,
        "network_request": True,
        "read_or_write": "READ_THEN_POSSIBLE_WRITE_REPLY",
        "credential": "TELEGRAM_BOT_TOKEN",
        "credential_status": telegram_token_status(env),
        "proposed_action": "setWebhook or getMe + one controlled inbound/outbound probe",
        "external_side_effect": "Telegram Bot API auth + possible message delivery",
        "reversible": True,
        "risk": "message delivery / token misuse if misconfigured",
    }


def voice_live_boundary(env: dict | None = None) -> dict:
    source = env if env is not None else os.environ
    return {
        "status": HUMAN_APPROVAL_REQUIRED,
        "network_request": True,
        "read_or_write": "READ",
        "credential": "speech provider key(s)",
        "credential_status": credential_status("OPENAI_API_KEY", source),  # common STT path; value never printed
        "proposed_action": "one offline-validated STT/TTS path with fixture; real provider only after approval",
        "voice_interface_enabled": voice_interface_enabled(source),
        "external_side_effect": "paid STT/TTS provider call",
        "reversible": True,
        "risk": "cost + audio privacy",
    }
