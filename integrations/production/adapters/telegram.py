"""Production Telegram Bot API adapter."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from b2b_commerce.errors import B2B_TELEGRAM_PROVIDER_FAILED, B2BCommerceError
from b2b_commerce.telegram import TelegramProvider, TelegramSendRequest, TelegramSendResult
from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient
from integrations.production.observability import ProviderObservability


@dataclass
class ProductionTelegramProvider:
    bot_token: str
    timeout_seconds: float = 15.0
    max_message_chars: int = 4096
    obs: ProviderObservability | None = None
    _http: BoundedHttpClient | None = None
    _sent_keys: set[str] = field(default_factory=set)
    sent: list[TelegramSendResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.bot_token:
            raise ProductionProviderError(
                ProviderErrorCategory.CONFIGURATION_ERROR,
                message="telegram_token_missing",
                provider_id="telegram",
            )
        # BoundedHttpClient is created lazily on first request — no network at construct.

    def __repr__(self) -> str:
        return "ProductionTelegramProvider(bot_token=[REDACTED])"

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    def send_message(self, request: TelegramSendRequest) -> TelegramSendResult:
        if request.idempotency_key in self._sent_keys:
            for item in self.sent:
                if item.chat_id == request.chat_id:
                    return item
        text = request.text[: self.max_message_chars]
        if not text.strip():
            raise B2BCommerceError(B2B_TELEGRAM_PROVIDER_FAILED, "empty message")
        started = time.monotonic()
        try:
            resp = self._http_client().request(
                "POST",
                f"{self.base_url}/sendMessage",
                json_body={"chat_id": request.chat_id, "text": text},
            )
            data = resp.json()
            if not data.get("ok"):
                raise B2BCommerceError(B2B_TELEGRAM_PROVIDER_FAILED, "telegram_api_error")
            msg_id = str(data.get("result", {}).get("message_id") or uuid.uuid4().hex[:10])
            result = TelegramSendResult(provider_message_id=f"tg_{msg_id}", status="sent", chat_id=request.chat_id)
            self._sent_keys.add(request.idempotency_key)
            self.sent.append(result)
            if self.obs:
                self.obs.emit(provider_id="telegram", operation="send_message", success=True, latency_ms=(time.monotonic() - started) * 1000)
            return result
        except ProductionProviderError as exc:
            if self.obs:
                self.obs.emit(provider_id="telegram", operation="send_message", success=False, error_category=exc.category.value)
            if exc.category == ProviderErrorCategory.RATE_LIMITED:
                raise B2BCommerceError(B2B_TELEGRAM_PROVIDER_FAILED, "rate limit") from exc
            raise B2BCommerceError(B2B_TELEGRAM_PROVIDER_FAILED, exc.message) from exc

    def health_check(self) -> dict:
        started = time.monotonic()
        try:
            resp = self._http_client().request("GET", f"{self.base_url}/getMe")
            data = resp.json()
            ok = bool(data.get("ok"))
            username = str(data.get("result", {}).get("username") or "")
            if self.obs:
                self.obs.emit(provider_id="telegram", operation="health", success=ok, latency_ms=(time.monotonic() - started) * 1000)
            return {"status": "healthy" if ok else "unhealthy", "bot_username": username}
        except ProductionProviderError as exc:
            return {"status": "unhealthy", "error_category": exc.category.value}

    def _http_client(self) -> BoundedHttpClient:
        if self._http is None:
            self._http = BoundedHttpClient(provider_id="telegram", timeout_seconds=self.timeout_seconds)
        return self._http

    def close(self) -> None:
        if self._http:
            self._http.close()


def verify_telegram_webhook(
    *,
    secret_token: str,
    header_token: str,
    raw_body: bytes,
    max_bytes: int = 65536,
) -> dict[str, Any]:
    import json

    if not secret_token or not hmac_safe_compare(secret_token, header_token):
        raise ProductionProviderError(ProviderErrorCategory.WEBHOOK_VERIFICATION_FAILED, provider_id="telegram")
    if len(raw_body) > max_bytes:
        raise ProductionProviderError(ProviderErrorCategory.BAD_REQUEST, message="payload_too_large", provider_id="telegram")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ProductionProviderError(ProviderErrorCategory.BAD_REQUEST, message="invalid_json", provider_id="telegram") from exc
    if "update_id" not in payload:
        raise ProductionProviderError(ProviderErrorCategory.BAD_REQUEST, message="missing_update_id", provider_id="telegram")
    return payload


def hmac_safe_compare(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(str(a), str(b))


def normalize_telegram_update(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message") or payload.get("edited_message") or {}
    chat = message.get("chat") or {}
    return {
        "update_id": str(payload.get("update_id")),
        "bot_id": str(payload.get("bot_id") or ""),
        "chat_id": str(chat.get("id") or ""),
        "text": str(message.get("text") or ""),
    }


def build_telegram_provider(env: dict) -> TelegramProvider | None:
    from b2b_commerce.providers.fake_telegram import FakeTelegramProvider

    enabled = str(env.get("TELEGRAM_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return FakeTelegramProvider()
    token = str(env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    prod = str(env.get("PANDA_ENV") or env.get("ENVIRONMENT") or "").strip().lower() in {"production", "prod"}
    if not token:
        if prod:
            raise ProductionProviderError(ProviderErrorCategory.CONFIGURATION_ERROR, message="telegram_token_required", provider_id="telegram")
        return FakeTelegramProvider()
    return ProductionTelegramProvider(bot_token=token)
