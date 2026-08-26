"""Requirement and offer normalization — no fabrication of missing values."""

from __future__ import annotations

from decimal import Decimal

from memory.models import utc_now
from procurement.models import (
    Money,
    OfferProvenance,
    ProcurementRequest,
    ProcurementRequirement,
    SupplierOffer,
    parse_money_amount,
)


def normalize_requirements(request: ProcurementRequest) -> ProcurementRequirement:
    missing = []
    if request.quantity is None:
        missing.append("quantity")
    if not request.unit:
        missing.append("unit")
    specs = dict(request.specifications or {})
    mandatory = {}
    preferred = {}
    for key, value in specs.items():
        if str(key).startswith("preferred_") or str(key).startswith("nice_"):
            preferred[str(key)] = value
        else:
            mandatory[str(key)] = value
    constraints = dict(request.constraints or {})
    compliance = {
        k: v for k, v in constraints.items() if "compliance" in str(k).lower() or "cert" in str(k).lower()
    }
    supplier_constraints = {
        k: v
        for k, v in constraints.items()
        if k not in compliance and k not in {"notes"}
    }
    currency = request.currency
    if request.target_budget is not None:
        currency = request.target_budget.currency
    incomplete = bool(missing)
    category = str(specs.get("category") or constraints.get("category") or "general")
    return ProcurementRequirement(
        category=category,
        normalized_item=str(request.item_name).strip(),
        quantity=request.quantity,
        unit=request.unit,
        mandatory_specs=mandatory,
        preferred_specs=preferred,
        budget_constraint=request.target_budget,
        currency=currency,
        delivery_deadline=request.required_by,
        delivery_location=request.delivery_location,
        supplier_constraints=supplier_constraints,
        compliance_constraints=compliance,
        notes=request.description,
        incomplete=incomplete,
        missing_fields=tuple(missing),
    )


class OfferNormalizer:
    """Normalize offer money fields without inventing zeros for unknowns."""

    def normalize(self, offer: SupplierOffer, *, now=None) -> SupplierOffer:
        stamp = now or utc_now()
        unit = offer.unit_price
        qty = offer.quantity
        subtotal = offer.subtotal
        if subtotal is None and unit is not None and qty is not None:
            subtotal = Money(amount=unit.amount * qty, currency=unit.currency)
        total = offer.total_cost
        # Only compute total when shipping/tax are known OR explicitly absent as None
        # and we have subtotal — still do NOT treat unknown shipping/tax as zero.
        shipping = offer.shipping_cost
        tax = offer.tax
        if total is None and subtotal is not None and shipping is None and tax is None:
            # total unknown if fees unknown — leave None (unknown != zero)
            total = None
        elif total is None and subtotal is not None and shipping is not None and tax is not None:
            if shipping.currency != subtotal.currency or tax.currency != subtotal.currency:
                total = None
            else:
                total = Money(
                    amount=subtotal.amount + shipping.amount + tax.amount,
                    currency=subtotal.currency,
                )
        status = offer.status
        if offer.valid_until is not None and offer.valid_until <= stamp:
            status = "expired"
        elif status == "discovered":
            status = "normalized"
        return SupplierOffer(
            offer_id=offer.offer_id,
            request_id=offer.request_id,
            supplier_id=offer.supplier_id,
            scope=offer.scope,
            source_type=offer.source_type,
            source_ref=offer.source_ref,
            currency=offer.currency,
            unit_price=unit,
            quantity=qty,
            provenance=offer.provenance,
            subtotal=subtotal,
            shipping_cost=shipping,
            tax=tax,
            total_cost=total,
            lead_time_days=offer.lead_time_days,
            minimum_order_quantity=offer.minimum_order_quantity,
            payment_terms=offer.payment_terms,
            delivery_terms=offer.delivery_terms,
            valid_until=offer.valid_until,
            availability=offer.availability,
            warranty=offer.warranty,
            specifications=dict(offer.specifications),
            compliance=dict(offer.compliance),
            confidence=offer.confidence,
            status=status,
            metadata_safe=dict(offer.metadata_safe),
            created_at=offer.created_at,
            updated_at=stamp,
        )

    def from_document_row(
        self,
        *,
        offer_id: str,
        request_id: str,
        supplier_id: str,
        scope,
        row: dict,
        provenance: OfferProvenance,
        source_type: str = "document",
        source_ref: str,
    ) -> SupplierOffer:
        currency = str(row.get("currency") or "").strip().upper()
        if not currency:
            raise ValueError("currency_required")
        unit_raw = row.get("unit_price")
        unit = Money(amount=parse_money_amount(unit_raw), currency=currency) if unit_raw is not None else None
        qty = row.get("quantity")
        quantity = parse_money_amount(qty) if qty is not None else None
        shipping = None
        if row.get("shipping_cost") is not None:
            shipping = Money(amount=parse_money_amount(row["shipping_cost"]), currency=currency)
        tax = None
        if row.get("tax") is not None:
            tax = Money(amount=parse_money_amount(row["tax"]), currency=currency)
        specs = row.get("specifications") if isinstance(row.get("specifications"), dict) else {}
        offer = SupplierOffer(
            offer_id=offer_id,
            request_id=request_id,
            supplier_id=supplier_id,
            scope=scope,
            source_type=source_type,
            source_ref=source_ref,
            currency=currency,
            unit_price=unit,
            quantity=quantity,
            provenance=provenance,
            shipping_cost=shipping,
            tax=tax,
            lead_time_days=int(row["lead_time_days"]) if row.get("lead_time_days") is not None else None,
            payment_terms=row.get("payment_terms"),
            delivery_terms=row.get("delivery_terms"),
            valid_until=row.get("valid_until"),
            availability=row.get("availability"),
            specifications=specs,
            compliance=row.get("compliance") if isinstance(row.get("compliance"), dict) else {},
            confidence=float(row["confidence"]) if row.get("confidence") is not None else 0.7,
            status="discovered",
            metadata_safe={"from_document": True},
        )
        return self.normalize(offer)
