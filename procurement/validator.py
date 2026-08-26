"""ProcurementValidator — completeness, provenance, scope, money safety."""

from __future__ import annotations

from procurement.errors import (
    PROCUREMENT_CURRENCY_MISMATCH,
    PROCUREMENT_MANDATORY_SPEC_FAILED,
    PROCUREMENT_OFFER_EXPIRED,
    PROCUREMENT_PROVENANCE_MISSING,
    PROCUREMENT_REQUEST_INVALID,
    PROCUREMENT_REQUIREMENTS_INCOMPLETE,
    PROCUREMENT_SUPPLIER_RESTRICTED,
    ProcurementError,
)
from procurement.models import Money, ProcurementRequest, SupplierOffer


_SECRET_MARKERS = (
    "GITHUB_WRITE_TOKEN",
    "PANDA_ENCRYPTION_KEY",
    "sk-",
    "ghp_",
    "Bearer ",
    "Authorization:",
)


class ProcurementValidator:
    def validate_request(self, request: ProcurementRequest) -> None:
        if not request.item_name.strip():
            raise ProcurementError(PROCUREMENT_REQUEST_INVALID)
        blob = " ".join(
            [
                request.item_name,
                request.description or "",
                str(dict(request.metadata_safe)),
            ]
        )
        if any(m in blob for m in _SECRET_MARKERS):
            raise ProcurementError(PROCUREMENT_REQUEST_INVALID, details={"reason": "secret_denied"})

    def validate_requirement(self, requirement) -> None:
        if requirement.incomplete:
            raise ProcurementError(
                PROCUREMENT_REQUIREMENTS_INCOMPLETE,
                details={"missing": list(requirement.missing_fields)},
            )

    def validate_offer(self, offer: SupplierOffer, *, now=None, require_provenance: bool = True) -> list[str]:
        issues = []
        if require_provenance and (
            not offer.provenance
            or not offer.provenance.content_hash
            or not offer.provenance.source_ref
            or dict(offer.metadata_safe or {}).get("price_provenance_missing")
        ):
            issues.append(PROCUREMENT_PROVENANCE_MISSING)
        if offer.unit_price is not None and not isinstance(offer.unit_price, Money):
            issues.append(PROCUREMENT_REQUEST_INVALID)
        if offer.unit_price is not None and isinstance(offer.unit_price.amount, float):
            issues.append(PROCUREMENT_REQUEST_INVALID)
        if now is not None and offer.valid_until is not None and offer.valid_until <= now:
            issues.append(PROCUREMENT_OFFER_EXPIRED)
        if offer.status == "expired":
            issues.append(PROCUREMENT_OFFER_EXPIRED)
        return issues

    def validate_recommendation(
        self,
        *,
        recommendation,
        offers: dict,
        suppliers: dict,
        policy,
    ) -> None:
        if recommendation.recommended_offer_id:
            offer = offers.get(recommendation.recommended_offer_id)
            if offer is None:
                raise ProcurementError(PROCUREMENT_REQUEST_INVALID, details={"reason": "unknown_offer"})
            if recommendation.recommended_supplier_id and offer.supplier_id != recommendation.recommended_supplier_id:
                raise ProcurementError(PROCUREMENT_REQUEST_INVALID, details={"reason": "supplier_offer_mismatch"})
            supplier = suppliers.get(offer.supplier_id)
            if supplier is not None and supplier.status == "restricted" and policy.exclude_restricted_suppliers:
                raise ProcurementError(PROCUREMENT_SUPPLIER_RESTRICTED)
            if offer.status == "expired" and policy.exclude_expired_offers:
                raise ProcurementError(PROCUREMENT_OFFER_EXPIRED)
            if "mandatory_spec_failed" in {
                f for row in recommendation.comparison if row.offer_id == offer.offer_id for f in row.flags
            }:
                # winner with mandatory fail is invalid
                for row in recommendation.comparison:
                    if row.offer_id == offer.offer_id and row.mandatory_spec_failed:
                        raise ProcurementError(PROCUREMENT_MANDATORY_SPEC_FAILED)
            if policy.require_price_provenance and (
                not offer.provenance
                or not offer.provenance.content_hash
                or dict(offer.metadata_safe or {}).get("price_provenance_missing")
            ):
                raise ProcurementError(PROCUREMENT_PROVENANCE_MISSING)

    def currencies_consistent(self, offers: tuple) -> bool:
        currencies = {o.currency for o in offers}
        return len(currencies) <= 1

    def assert_no_fx_needed_or_flag(self, offers: tuple) -> bool:
        """Return True if conversion required."""
        return not self.currencies_consistent(offers)
