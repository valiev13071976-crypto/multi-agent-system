"""Transactional email production adapter."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from integrations.production.errors import ProductionProviderError, ProviderErrorCategory
from integrations.production.http import BoundedHttpClient
from integrations.production.observability import ProviderObservability

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HEADER_INJECTION = re.compile(r"[\r\n]")


class TransactionalEmailProvider(Protocol):
    provider_id: str

    def send(self, message: "TransactionalEmailMessage") -> "EmailDeliveryResult": ...

    def health_check(self) -> dict: ...


@dataclass(frozen=True)
class TransactionalEmailMessage:
    recipient: str
    event_type: str
    template_data: dict[str, Any]
    idempotency_key: str
    tenant_id: str = ""
    user_id: str = ""
    correlation_id: str = ""


@dataclass
class EmailDeliveryResult:
    provider_message_id: str
    status: str
    idempotency_key: str


@dataclass
class ResendEmailProvider:
    provider_id: str = "resend"
    api_key: str = ""
    from_address: str = ""
    timeout_seconds: float = 20.0
    obs: ProviderObservability | None = None
    _http: BoundedHttpClient | None = None
    _sent: dict[str, EmailDeliveryResult] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.api_key or not self.from_address:
            raise ProductionProviderError(
                ProviderErrorCategory.CONFIGURATION_ERROR,
                message="email_not_configured",
                provider_id="email",
            )
        self._http = BoundedHttpClient(provider_id="email", timeout_seconds=self.timeout_seconds)

    def send(self, message: TransactionalEmailMessage) -> EmailDeliveryResult:
        self._validate(message)
        existing = self._sent.get(message.idempotency_key)
        if existing:
            return existing
        subject, body = self._render(message)
        started = time.monotonic()
        resp = self._http.request(
            "POST",
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json_body={"from": self.from_address, "to": [message.recipient], "subject": subject, "text": body},
        )
        data = resp.json()
        result = EmailDeliveryResult(
            provider_message_id=str(data.get("id") or f"email-{uuid.uuid4().hex[:12]}"),
            status="accepted",
            idempotency_key=message.idempotency_key,
        )
        self._sent[message.idempotency_key] = result
        if self.obs:
            self.obs.emit(provider_id="email", operation=message.event_type, success=True, latency_ms=(time.monotonic() - started) * 1000)
        return result

    def health_check(self) -> dict:
        return {"status": "configured", "provider": self.provider_id}

    def _validate(self, message: TransactionalEmailMessage) -> None:
        if not _EMAIL_RE.match(message.recipient):
            raise ProductionProviderError(ProviderErrorCategory.BAD_REQUEST, message="invalid_recipient", provider_id="email")
        for val in (message.recipient, message.event_type, message.tenant_id):
            if _HEADER_INJECTION.search(str(val)):
                raise ProductionProviderError(ProviderErrorCategory.BAD_REQUEST, message="header_injection", provider_id="email")

    def _render(self, message: TransactionalEmailMessage) -> tuple[str, str]:
        if message.event_type == "tenant_invitation":
            subject = "You have been invited"
            body = "You have been invited to join a workspace. Use your invitation link to accept."
            return subject, body
        if message.event_type == "privacy_export_complete":
            return "Privacy export ready", "Your privacy export is ready for download."
        return f"Notification: {message.event_type}", "You have a new notification."

    def close(self) -> None:
        if self._http:
            self._http.close()


@dataclass
class FakeTransactionalEmailProvider:
    provider_id: str = "fake_email"
    sent: list[TransactionalEmailMessage] = field(default_factory=list)
    _results: dict[str, EmailDeliveryResult] = field(default_factory=dict)
    fail_transient: bool = False
    fail_permanent: bool = False
    _attempts: dict[str, int] = field(default_factory=dict)

    def send(self, message: TransactionalEmailMessage) -> EmailDeliveryResult:
        if _HEADER_INJECTION.search(message.recipient):
            raise ProductionProviderError(ProviderErrorCategory.BAD_REQUEST, message="header_injection", provider_id="email")
        attempts = self._attempts.get(message.idempotency_key, 0) + 1
        self._attempts[message.idempotency_key] = attempts
        if message.idempotency_key in self._results:
            return self._results[message.idempotency_key]
        if self.fail_permanent:
            raise ProductionProviderError(ProviderErrorCategory.BAD_REQUEST, message="permanent_reject", provider_id="email")
        if self.fail_transient and attempts == 1:
            raise ProductionProviderError(ProviderErrorCategory.PROVIDER_UNAVAILABLE, message="transient", provider_id="email", retryable=True)
        self.sent.append(message)
        result = EmailDeliveryResult(provider_message_id=f"fake-{uuid.uuid4().hex[:10]}", status="accepted", idempotency_key=message.idempotency_key)
        self._results[message.idempotency_key] = result
        return result

    def health_check(self) -> dict:
        return {"status": "healthy", "provider": self.provider_id}


def build_email_provider(env: dict) -> TransactionalEmailProvider:
    enabled = str(env.get("EMAIL_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return FakeTransactionalEmailProvider()
    key = str(env.get("EMAIL_API_KEY") or "").strip()
    from_addr = str(env.get("EMAIL_FROM_ADDRESS") or "").strip()
    prod = str(env.get("PANDA_ENV") or env.get("ENVIRONMENT") or "").strip().lower() in {"production", "prod"}
    if not key or not from_addr:
        if prod:
            raise ProductionProviderError(ProviderErrorCategory.CONFIGURATION_ERROR, message="email_credentials_required", provider_id="email")
        return FakeTransactionalEmailProvider()
    return ResendEmailProvider(api_key=key, from_address=from_addr)
