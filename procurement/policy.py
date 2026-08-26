"""Versioned procurement policy and scoring weights."""

from __future__ import annotations

from decimal import Decimal

from procurement.models import PROCUREMENT_POLICY_VERSION, PROCUREMENT_SCORING_VERSION


class ProcurementPolicy:
    policy_version = PROCUREMENT_POLICY_VERSION

    def __init__(
        self,
        *,
        minimum_valid_offers: int = 2,
        allow_single_source_exception: bool = True,
        require_price_provenance: bool = True,
        exclude_restricted_suppliers: bool = True,
        exclude_expired_offers: bool = True,
        approval_required: bool = True,
        max_single_order_amount: Decimal | None = None,
        required_comparison_fields: tuple[str, ...] = (
            "unit_price",
            "currency",
            "specifications",
            "provenance",
        ),
    ):
        self.minimum_valid_offers = int(minimum_valid_offers)
        self.allow_single_source_exception = bool(allow_single_source_exception)
        self.require_price_provenance = bool(require_price_provenance)
        self.exclude_restricted_suppliers = bool(exclude_restricted_suppliers)
        self.exclude_expired_offers = bool(exclude_expired_offers)
        self.approval_required = bool(approval_required)
        self.max_single_order_amount = max_single_order_amount
        self.required_comparison_fields = tuple(required_comparison_fields)


class ProcurementScoringPolicy:
    scoring_version = PROCUREMENT_SCORING_VERSION

    def __init__(
        self,
        *,
        weight_spec_match: Decimal = Decimal("3.0"),
        weight_total_cost: Decimal = Decimal("2.0"),
        weight_delivery: Decimal = Decimal("1.0"),
        weight_trust: Decimal = Decimal("1.5"),
        weight_freshness: Decimal = Decimal("0.8"),
        weight_compliance: Decimal = Decimal("1.2"),
        weight_provenance: Decimal = Decimal("1.5"),
        weight_risk: Decimal = Decimal("2.0"),
        unknown_penalty: Decimal = Decimal("1.0"),
        mandatory_fail_cap: Decimal = Decimal("0"),
    ):
        self.weight_spec_match = Decimal(str(weight_spec_match))
        self.weight_total_cost = Decimal(str(weight_total_cost))
        self.weight_delivery = Decimal(str(weight_delivery))
        self.weight_trust = Decimal(str(weight_trust))
        self.weight_freshness = Decimal(str(weight_freshness))
        self.weight_compliance = Decimal(str(weight_compliance))
        self.weight_provenance = Decimal(str(weight_provenance))
        self.weight_risk = Decimal(str(weight_risk))
        self.unknown_penalty = Decimal(str(unknown_penalty))
        self.mandatory_fail_cap = Decimal(str(mandatory_fail_cap))

    def as_dict(self) -> dict:
        return {
            "procurement_scoring_version": self.scoring_version,
            "weights": {
                "spec_match": str(self.weight_spec_match),
                "total_cost": str(self.weight_total_cost),
                "delivery": str(self.weight_delivery),
                "trust": str(self.weight_trust),
                "freshness": str(self.weight_freshness),
                "compliance": str(self.weight_compliance),
                "provenance": str(self.weight_provenance),
                "risk": str(self.weight_risk),
                "unknown_penalty": str(self.unknown_penalty),
                "mandatory_fail_cap": str(self.mandatory_fail_cap),
            },
        }


def procurement_policy_snapshot() -> dict:
    return {
        "procurement_policy_version": PROCUREMENT_POLICY_VERSION,
        "procurement_scoring_version": PROCUREMENT_SCORING_VERSION,
        "minimum_valid_offers_default": 2,
        "allow_single_source_exception": True,
        "require_price_provenance": True,
        "exclude_restricted_suppliers": True,
        "exclude_expired_offers": True,
        "approval_required": True,
        "no_purchase_execution": True,
        "no_payment_execution": True,
        "no_auto_fx": True,
        "unknown_cost_not_zero": True,
        "rules": [
            "scoped_access_only",
            "no_fabricated_requirements",
            "knowledge_first",
            "mandatory_specs_beat_price",
            "restricted_suppliers_excluded",
            "hitl_before_protected_action",
            "no_public_api",
        ],
    }
