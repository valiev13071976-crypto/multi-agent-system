"""External alert delivery boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AlertDeliveryResult:
    delivered: bool
    sink: str
    error: str = ""


class AlertSink:
    """Provider-neutral alert delivery."""

    def deliver(self, *, code: str, severity: str, message: str, details: dict[str, Any] | None = None) -> AlertDeliveryResult:
        raise NotImplementedError


class FakeAlertSink(AlertSink):
    def __init__(self):
        self.deliveries: list[dict[str, Any]] = []

    def deliver(self, *, code: str, severity: str, message: str, details: dict[str, Any] | None = None) -> AlertDeliveryResult:
        payload = {"code": code, "severity": severity, "message": message, "details": dict(details or {})}
        self.deliveries.append(payload)
        return AlertDeliveryResult(delivered=True, sink="fake")


class WebhookAlertSink(AlertSink):
    def __init__(self, url: str):
        self.url = url

    def deliver(self, *, code: str, severity: str, message: str, details: dict[str, Any] | None = None) -> AlertDeliveryResult:
        if not self.url:
            return AlertDeliveryResult(delivered=False, sink="webhook", error="not_configured")
        return AlertDeliveryResult(delivered=False, sink="webhook", error="live_delivery_operator_required")


def build_alert_sink(env: dict | None = None) -> AlertSink:
    from production_foundation.config import resolve_production_config

    cfg = resolve_production_config(env)
    if cfg.alert_webhook_url:
        return WebhookAlertSink(cfg.alert_webhook_url)
    return FakeAlertSink()
