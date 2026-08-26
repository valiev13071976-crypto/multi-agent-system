"""Deterministic procurement risk analysis."""

from __future__ import annotations

from memory.models import utc_now
from procurement.models import (
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RiskFinding,
)


class ProcurementRiskAnalyzer:
    def analyze(
        self,
        *,
        offers: tuple,
        suppliers: dict,
        requirement,
        comparison: tuple = (),
        single_source: bool = False,
        currency_conversion_required: bool = False,
        now=None,
    ) -> tuple[RiskFinding, ...]:
        stamp = now or utc_now()
        findings: list[RiskFinding] = []

        if requirement.incomplete:
            findings.append(
                RiskFinding(
                    category="specification",
                    level=RISK_HIGH,
                    code="requirements_incomplete",
                    message="Mandatory request fields missing",
                )
            )

        if single_source:
            findings.append(
                RiskFinding(
                    category="concentration",
                    level=RISK_HIGH,
                    code="single_source_procurement",
                    message="Only one valid supplier/offer available",
                )
            )

        if currency_conversion_required:
            findings.append(
                RiskFinding(
                    category="price",
                    level=RISK_CRITICAL,
                    code="currency_conversion_required",
                    message="Offers span multiple currencies without FX",
                )
            )

        for offer in offers:
            supplier = suppliers.get(offer.supplier_id)
            if supplier is not None and supplier.status == "restricted":
                findings.append(
                    RiskFinding(
                        category="supplier",
                        level=RISK_CRITICAL,
                        code="restricted_supplier",
                        message="Supplier is restricted",
                        offer_id=offer.offer_id,
                        supplier_id=offer.supplier_id,
                    )
                )
            expired = offer.valid_until is not None and offer.valid_until <= stamp
            if expired or offer.status == "expired":
                findings.append(
                    RiskFinding(
                        category="freshness",
                        level=RISK_CRITICAL,
                        code="offer_expired",
                        message="Offer validity expired",
                        offer_id=offer.offer_id,
                        supplier_id=offer.supplier_id,
                    )
                )
            if not offer.provenance or not offer.provenance.content_hash:
                findings.append(
                    RiskFinding(
                        category="data_quality",
                        level=RISK_CRITICAL,
                        code="provenance_missing",
                        message="Price provenance missing",
                        offer_id=offer.offer_id,
                        supplier_id=offer.supplier_id,
                    )
                )
            if offer.shipping_cost is None or offer.tax is None:
                findings.append(
                    RiskFinding(
                        category="unknowns",
                        level=RISK_MEDIUM,
                        code="unknown_fees",
                        message="Shipping/tax unknown — not treated as zero",
                        offer_id=offer.offer_id,
                        supplier_id=offer.supplier_id,
                    )
                )
            if offer.lead_time_days is None:
                findings.append(
                    RiskFinding(
                        category="delivery",
                        level=RISK_MEDIUM,
                        code="unknown_lead_time",
                        message="Lead time unknown",
                        offer_id=offer.offer_id,
                        supplier_id=offer.supplier_id,
                    )
                )

        for row in comparison:
            if row.mandatory_spec_failed:
                findings.append(
                    RiskFinding(
                        category="specification",
                        level=RISK_CRITICAL,
                        code="mandatory_spec_failed",
                        message="Mandatory specification not met",
                        offer_id=row.offer_id,
                        supplier_id=row.supplier_id,
                    )
                )

        if requirement.delivery_deadline is not None:
            for offer in offers:
                if offer.lead_time_days is not None:
                    # rough: if lead time absurdly long relative to now→deadline
                    delta = (requirement.delivery_deadline - stamp).total_seconds() / 86400.0
                    if delta >= 0 and offer.lead_time_days > int(delta) + 1:
                        findings.append(
                            RiskFinding(
                                category="delivery",
                                level=RISK_HIGH,
                                code="impossible_delivery_deadline",
                                message="Lead time exceeds delivery deadline",
                                offer_id=offer.offer_id,
                                supplier_id=offer.supplier_id,
                            )
                        )

        if not findings:
            findings.append(
                RiskFinding(
                    category="data_quality",
                    level=RISK_LOW,
                    code="no_critical_risks",
                    message="No elevated risks detected",
                )
            )
        return tuple(findings)

    @staticmethod
    def has_critical(findings: tuple[RiskFinding, ...], *, offer_id: str | None = None) -> bool:
        for f in findings:
            if f.level != RISK_CRITICAL:
                continue
            if offer_id is None or f.offer_id in {None, offer_id}:
                if f.code != "no_critical_risks":
                    return True
        return False
