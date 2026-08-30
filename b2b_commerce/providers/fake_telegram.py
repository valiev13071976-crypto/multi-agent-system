"""Fake Telegram provider for closure tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from b2b_commerce.errors import B2B_TELEGRAM_PROVIDER_FAILED, B2BCommerceError
from b2b_commerce.telegram import TelegramProvider, TelegramSendRequest, TelegramSendResult


@dataclass
class FakeTelegramProvider(TelegramProvider):
    rate_limit_first: bool = False
    invalid_chats: set[str] = field(default_factory=set)
    sent: list[TelegramSendResult] = field(default_factory=list)
    _attempts: dict[str, int] = field(default_factory=dict)

    def send_message(self, request: TelegramSendRequest) -> TelegramSendResult:
        if request.chat_id in self.invalid_chats:
            raise B2BCommerceError(B2B_TELEGRAM_PROVIDER_FAILED, "invalid chat")
        if request.idempotency_key in self._attempts and self.sent:
            for item in reversed(self.sent):
                if item.chat_id == request.chat_id:
                    return item
        attempts = self._attempts.get(request.idempotency_key, 0) + 1
        self._attempts[request.idempotency_key] = attempts
        if self.rate_limit_first and attempts == 1:
            raise B2BCommerceError(B2B_TELEGRAM_PROVIDER_FAILED, "rate limit")
        result = TelegramSendResult(
            provider_message_id=f"tg_{uuid.uuid4().hex[:10]}",
            status="sent",
            chat_id=request.chat_id,
        )
        self.sent.append(result)
        return result
