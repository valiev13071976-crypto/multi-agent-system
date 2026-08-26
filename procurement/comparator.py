"""Deterministic supplier/offer comparison with versioned weights."""

from __future__ import annotations

from decimal import Decimal

from memory.models import utc_now
from procurement.models import ComparisonRow, Money
from procurement.policy import ProcurementScoringPolicy


TRUST_SCORE = {
    "known_internal": Decimal("1.0"),
    "document_sourced": Decimal("0.8"),
    "read_only_external": Decimal("0.5"),
    "unverified_external": Decimal("0.3"),
}


class SupplierComparator:
    def __init__(self, scoring: ProcurementScoringPolicy | None = None):
        self.scoring = scoring or ProcurementScoringPolicy()

    def compare(
        self,
        *,
        offers: tuple,
        suppliers: dict,
        requirement,
        now=None,
    ) -> tuple[ComparisonRow, ...]:
        stamp = now or utc_now()
        currencies = {o.currency for o in offers if o.currency}
        multi_currency = len(currencies) > 1

        # Cost ranking only within same currency groups
        cost_by_currency: dict[str, list] = {}
        for offer in offers:
            cost = self._comparable_cost(offer)
            if cost is not None:
                cost_by_currency.setdefault(offer.currency, []).append(cost.amount)

        rows = []
        for offer in offers:
            supplier = suppliers.get(offer.supplier_id)
            flags = []
            mandatory_fail = self._mandatory_spec_failed(offer, requirement)
            if mandatory_fail:
                flags.append("mandatory_spec_failed")

            expired = offer.valid_until is not None and offer.valid_until <= stamp
            if expired or offer.status == "expired":
                flags.append("expired")

            if supplier is not None and supplier.status == "restricted":
                flags.append("restricted_supplier")

            meta = dict(offer.metadata_safe or {})
            provenance_ok = bool(
                not meta.get("price_provenance_missing")
                and offer.provenance
                and offer.provenance.content_hash
                and offer.provenance.source_ref
            )
            if not provenance_ok:
                flags.append("provenance_missing")

            unknown_fees = offer.shipping_cost is None or offer.tax is None
            if unknown_fees:
                flags.append("unknown_fees")

            if multi_currency:
                flags.append("currency_conversion_required")

            spec_score = Decimal("0") if mandatory_fail else self._spec_score(offer, requirement)
            cost_score = self._cost_score(offer, cost_by_currency.get(offer.currency, []))
            if multi_currency:
                cost_score = Decimal("0")  # do not cross-compare currencies
            delivery_score = self._delivery_score(offer)
            trust_score = TRUST_SCORE.get(
                (supplier.trust_level if supplier else offer.provenance.trust),
                Decimal("0.3"),
            )
            freshness_score = Decimal("0.4") if "stale" in (offer.provenance.freshness or "") else Decimal("0.8")
            compliance_score = Decimal("1.0") if dict(offer.compliance) else Decimal("0.5")
            provenance_score = Decimal("1.0") if provenance_ok else Decimal("0")
            risk_penalty = Decimal("0")
            if mandatory_fail:
                risk_penalty += Decimal("5")
            if "restricted_supplier" in flags:
                risk_penalty += Decimal("10")
            if "expired" in flags:
                risk_penalty += Decimal("5")
            if "provenance_missing" in flags:
                risk_penalty += Decimal("4")
            if unknown_fees:
                risk_penalty += self.scoring.unknown_penalty

            s = self.scoring
            score = (
                spec_score * s.weight_spec_match
                + cost_score * s.weight_total_cost
                + delivery_score * s.weight_delivery
                + trust_score * s.weight_trust
                + freshness_score * s.weight_freshness
                + compliance_score * s.weight_compliance
                + provenance_score * s.weight_provenance
                - risk_penalty * s.weight_risk
            )
            if mandatory_fail:
                score = min(score, s.mandatory_fail_cap)

            total_or_unit = None
            if offer.total_cost is not None:
                total_or_unit = str(offer.total_cost.amount)
            elif offer.unit_price is not None:
                total_or_unit = str(offer.unit_price.amount)

            rows.append(
                ComparisonRow(
                    offer_id=offer.offer_id,
                    supplier_id=offer.supplier_id,
                    score=score,
                    rank=0,
                    currency=offer.currency,
                    total_or_unit=total_or_unit,
                    mandatory_spec_failed=mandatory_fail,
                    flags=tuple(flags),
                    breakdown={
                        "spec": str(spec_score),
                        "cost": str(cost_score),
                        "delivery": str(delivery_score),
                        "trust": str(trust_score),
                        "risk_penalty": str(risk_penalty),
                    },
                )
            )

        rows.sort(key=lambda r: (-r.score, r.offer_id))
        ranked = []
        for i, row in enumerate(rows, start=1):
            ranked.append(
                ComparisonRow(
                    offer_id=row.offer_id,
                    supplier_id=row.supplier_id,
                    score=row.score,
                    rank=i,
                    currency=row.currency,
                    total_or_unit=row.total_or_unit,
                    mandatory_spec_failed=row.mandatory_spec_failed,
                    flags=row.flags,
                    breakdown=dict(row.breakdown),
                )
            )
        return tuple(ranked)

    def _comparable_cost(self, offer) -> Money | None:
        if offer.total_cost is not None:
            return offer.total_cost
        if offer.subtotal is not None:
            return offer.subtotal
        if offer.unit_price is not None and offer.quantity is not None:
            return Money(amount=offer.unit_price.amount * offer.quantity, currency=offer.currency)
        if offer.unit_price is not None:
            return offer.unit_price
        return None

    def _cost_score(self, offer, peer_amounts: list) -> Decimal:
        cost = self._comparable_cost(offer)
        if cost is None or not peer_amounts:
            return Decimal("0")
        best = min(peer_amounts)
        worst = max(peer_amounts)
        if worst == best:
            return Decimal("1")
        # Higher score for lower cost
        return (worst - cost.amount) / (worst - best)

    def _delivery_score(self, offer) -> Decimal:
        if offer.lead_time_days is None:
            return Decimal("0")  # unknown != best
        days = int(offer.lead_time_days)
        if days <= 7:
            return Decimal("1.0")
        if days <= 14:
            return Decimal("0.7")
        if days <= 30:
            return Decimal("0.4")
        return Decimal("0.2")

    def _spec_score(self, offer, requirement) -> Decimal:
        mandatory = dict(requirement.mandatory_specs or {})
        if not mandatory:
            return Decimal("0.8")
        specs = {str(k).lower(): str(v).lower() for k, v in dict(offer.specifications or {}).items()}
        matched = 0
        for key, value in mandatory.items():
            if specs.get(str(key).lower()) == str(value).lower():
                matched += 1
        return Decimal(matched) / Decimal(len(mandatory))

    def _mandatory_spec_failed(self, offer, requirement) -> bool:
        mandatory = dict(requirement.mandatory_specs or {})
        if not mandatory:
            return False
        specs = {str(k).lower(): str(v).lower() for k, v in dict(offer.specifications or {}).items()}
        for key, value in mandatory.items():
            if specs.get(str(key).lower()) != str(value).lower():
                return True
        return False
