"""Operator alerts with deduplication."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from marketplace.models import ALERT_OPEN, MarketplaceOperatorAlert


@dataclass
class AlertStore:
    _by_issue: dict[str, MarketplaceOperatorAlert] = field(default_factory=dict)
    _all: list[MarketplaceOperatorAlert] = field(default_factory=list)

    def upsert(
        self,
        *,
        tenant_id: str,
        provider: str,
        account_id: str,
        alert_type: str,
        sku_id: str,
        summary: str,
        evidence: tuple[str, ...],
        severity: str = "HIGH",
        financial_impact: str = "",
        recommended_action: str = "",
        auto_correction_available: bool = False,
    ) -> MarketplaceOperatorAlert:
        issue_key = f"{tenant_id}:{provider}:{account_id}:{alert_type}:{sku_id}"
        existing = self._by_issue.get(issue_key)
        if existing and existing.status == ALERT_OPEN:
            return existing
        alert = MarketplaceOperatorAlert(
            alert_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider=provider,
            account_id=account_id,
            severity=severity,
            alert_type=alert_type,
            sku_id=sku_id,
            summary=summary,
            evidence=evidence,
            financial_impact=financial_impact,
            recommended_action=recommended_action,
            auto_correction_available=auto_correction_available,
            issue_key=issue_key,
        )
        self._by_issue[issue_key] = alert
        self._all.append(alert)
        return alert

    def list_for_tenant(self, tenant_id: str) -> list[MarketplaceOperatorAlert]:
        return [a for a in self._all if a.tenant_id == tenant_id]
