"""Versioned deterministic payment policy engine — not prompt-only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from autonomy.models import sanitize_metadata


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PaymentPolicy:
    policy_id: str
    version: str
    effective_from: datetime
    amount_tolerance: float = 0.01
    auto_match_confidence_threshold: float = 0.85
    allowed_payment_methods: tuple[str, ...] = ("card_token_ref", "bank_transfer", "invoice")
    allow_partial_fulfillment: bool = False
    overpayment_behavior: str = "review"  # review | credit | refund_proposal
    fulfillment_requires_confirmed: bool = True
    refund_approval_threshold: float = 0.0  # any refund >= threshold needs HITL (0 = all)
    payer_mismatch_behavior: str = "review"  # review | block
    chargeback_behavior: str = "human_review"
    date_window_days: int = 7
    multi_invoice_allocation: bool = True
    currency_strict: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self, "allowed_payment_methods", tuple(self.allowed_payment_methods)
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(sanitize_metadata(dict(self.metadata or {}))))
        )


DEFAULT_POLICY = PaymentPolicy(
    policy_id="payments.default",
    version="1.0.0",
    effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
)


class PaymentPolicyEngine:
    def __init__(self, policies: list[PaymentPolicy] | None = None):
        self._policies = list(policies or [DEFAULT_POLICY])

    def register(self, policy: PaymentPolicy) -> None:
        self._policies.append(policy)

    def active(self, *, at: datetime | None = None) -> PaymentPolicy:
        stamp = at or _utc()
        eligible = [p for p in self._policies if p.effective_from <= stamp]
        if not eligible:
            return DEFAULT_POLICY
        eligible.sort(key=lambda p: (p.effective_from, p.version), reverse=True)
        return eligible[0]

    def within_tolerance(self, expected: float, actual: float, policy: PaymentPolicy | None = None) -> bool:
        pol = policy or self.active()
        return abs(float(expected) - float(actual)) <= float(pol.amount_tolerance)

    def refund_requires_hitl(self, amount: float, *, policy: PaymentPolicy | None = None) -> bool:
        pol = policy or self.active()
        return float(amount) >= float(pol.refund_approval_threshold)
