"""Recommendation builder — comparator + risk + policy, not lowest price alone."""

from __future__ import annotations

import uuid

from memory.models import utc_now
from procurement.comparator import SupplierComparator
from procurement.models import ProcurementRecommendation
from procurement.policy import ProcurementPolicy
from procurement.risk import ProcurementRiskAnalyzer
from procurement.validator import ProcurementValidator


class ProcurementRecommendationService:
    def __init__(
        self,
        *,
        comparator: SupplierComparator | None = None,
        risk_analyzer: ProcurementRiskAnalyzer | None = None,
        validator: ProcurementValidator | None = None,
        policy: ProcurementPolicy | None = None,
    ):
        self.comparator = comparator or SupplierComparator()
        self.risk_analyzer = risk_analyzer or ProcurementRiskAnalyzer()
        self.validator = validator or ProcurementValidator()
        self.policy = policy or ProcurementPolicy()

    def build(
        self,
        *,
        request,
        requirement,
        offers: tuple,
        suppliers: dict,
        citations: tuple[str, ...] = (),
        now=None,
    ) -> ProcurementRecommendation:
        stamp = now or utc_now()
        currency_conversion_required = self.validator.assert_no_fx_needed_or_flag(offers)

        # Filter eligibility for winner selection
        eligible = []
        for offer in offers:
            supplier = suppliers.get(offer.supplier_id)
            if supplier is not None and supplier.status == "restricted" and self.policy.exclude_restricted_suppliers:
                continue
            expired = offer.valid_until is not None and offer.valid_until <= stamp
            if (expired or offer.status == "expired") and self.policy.exclude_expired_offers:
                continue
            if self.policy.require_price_provenance and (
                not offer.provenance
                or not offer.provenance.content_hash
                or dict(offer.metadata_safe or {}).get("price_provenance_missing")
            ):
                continue
            eligible.append(offer)

        comparison = self.comparator.compare(
            offers=tuple(offers),
            suppliers=suppliers,
            requirement=requirement,
            now=stamp,
        )

        # Pick best eligible without mandatory_spec_failed / critical flags
        winner = None
        for row in comparison:
            if row.offer_id not in {o.offer_id for o in eligible}:
                continue
            if row.mandatory_spec_failed:
                continue
            if "restricted_supplier" in row.flags:
                continue
            if "expired" in row.flags:
                continue
            if "provenance_missing" in row.flags:
                continue
            if currency_conversion_required:
                # still allow recommendation within a currency group: prefer first eligible
                pass
            winner = row
            break

        single_source = len(eligible) < self.policy.minimum_valid_offers
        if single_source and not self.policy.allow_single_source_exception:
            winner = None

        risks = self.risk_analyzer.analyze(
            offers=tuple(offers),
            suppliers=suppliers,
            requirement=requirement,
            comparison=comparison,
            single_source=single_source and len(eligible) == 1,
            currency_conversion_required=currency_conversion_required,
            now=stamp,
        )

        # Critical risk on winner blocks "safe" selection
        if winner is not None and self.risk_analyzer.has_critical(risks, offer_id=winner.offer_id):
            # allow recommendation but mark requires_approval and lower confidence
            pass

        alternatives = tuple(
            row.offer_id for row in comparison if winner is None or row.offer_id != winner.offer_id
        )[:5]

        assumptions = []
        missing = list(requirement.missing_fields)
        if any("unknown_fees" in row.flags for row in comparison):
            assumptions.append("unknown_shipping_or_tax_not_treated_as_zero")
        if currency_conversion_required:
            assumptions.append("no_live_fx_conversion")
            missing.append("currency_conversion")

        reasoning_parts = []
        if winner is None:
            reasoning_parts.append("No safe winning offer under policy constraints.")
        else:
            reasoning_parts.append(
                f"Selected offer {winner.offer_id} by deterministic score {winner.score}."
            )
            if winner.mandatory_spec_failed:
                reasoning_parts.append("Warning: mandatory specs failed.")
        if single_source and len(eligible) == 1:
            reasoning_parts.append("Single-source procurement flagged.")
        if currency_conversion_required:
            reasoning_parts.append("Currency conversion required; totals not cross-compared.")

        confidence = 0.75
        if winner is None:
            confidence = 0.2
        elif single_source:
            confidence = 0.45
        if currency_conversion_required:
            confidence = min(confidence, 0.4)
        if self.risk_analyzer.has_critical(risks, offer_id=winner.offer_id if winner else None):
            confidence = min(confidence, 0.35)

        rec = ProcurementRecommendation(
            recommendation_id=str(uuid.uuid4()),
            request_id=request.request_id,
            scope=request.scope,
            recommended_supplier_id=winner.supplier_id if winner else None,
            recommended_offer_id=winner.offer_id if winner else None,
            alternatives=alternatives,
            reasoning_summary=" ".join(reasoning_parts),
            comparison=comparison,
            risks=risks,
            assumptions=tuple(assumptions),
            missing_information=tuple(dict.fromkeys(missing)),
            confidence=confidence,
            citations=tuple(citations),
            requires_approval=self.policy.approval_required,
            status="recommendation_ready" if winner else "failed",
            single_source_procurement=bool(single_source and len(eligible) == 1),
            currency_conversion_required=currency_conversion_required,
            metadata_safe={
                "eligible_count": len(eligible),
                "offer_count": len(offers),
            },
            created_at=stamp,
        )
        offer_map = {o.offer_id: o for o in offers}
        if winner is not None:
            self.validator.validate_recommendation(
                recommendation=rec,
                offers=offer_map,
                suppliers=suppliers,
                policy=self.policy,
            )
        return rec
