"""Canonical procurement error reasons."""

from __future__ import annotations


class ProcurementError(RuntimeError):
    def __init__(self, reason: str, *, details: dict | None = None):
        self.reason = reason
        self.details = dict(details or {})
        super().__init__(reason)


PROCUREMENT_REQUEST_INVALID = "procurement_request_invalid"
PROCUREMENT_SCOPE_DENIED = "procurement_scope_denied"
PROCUREMENT_REQUIREMENTS_INCOMPLETE = "procurement_requirements_incomplete"
PROCUREMENT_NO_SUPPLIERS = "procurement_no_suppliers"
PROCUREMENT_NO_VALID_OFFERS = "procurement_no_valid_offers"
PROCUREMENT_CURRENCY_MISMATCH = "procurement_currency_mismatch"
PROCUREMENT_OFFER_EXPIRED = "procurement_offer_expired"
PROCUREMENT_SUPPLIER_RESTRICTED = "procurement_supplier_restricted"
PROCUREMENT_MANDATORY_SPEC_FAILED = "procurement_mandatory_spec_failed"
PROCUREMENT_PROVENANCE_MISSING = "procurement_provenance_missing"
PROCUREMENT_POLICY_DENIED = "procurement_policy_denied"
PROCUREMENT_APPROVAL_REQUIRED = "procurement_approval_required"
PROCUREMENT_ACTION_DENIED = "procurement_action_denied"
