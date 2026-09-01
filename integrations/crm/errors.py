"""CRM integration errors."""

from __future__ import annotations

from integrations.activation.errors import ActivationError


class CrmIntegrationError(ActivationError):
    code = "CRM_INTEGRATION_ERROR"


class CrmAmbiguousTargetError(CrmIntegrationError):
    code = "INTEGRATION_AMBIGUOUS_TARGET"


class CrmDuplicateCandidateError(CrmIntegrationError):
    code = "VALIDATION_FAILED"


class CrmUncertainWriteOutcomeError(CrmIntegrationError):
    code = "INTEGRATION_UNCERTAIN_WRITE_OUTCOME"


class CrmUnsupportedCapabilityError(CrmIntegrationError):
    code = "INTEGRATION_CAPABILITY_UNAVAILABLE"


class CrmNotFoundError(CrmIntegrationError):
    code = "INTEGRATION_NOT_FOUND"
