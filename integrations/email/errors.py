"""Email integration errors."""

from __future__ import annotations

from integrations.activation.errors import ActivationError


class EmailIntegrationError(ActivationError):
    code = "EMAIL_INTEGRATION_ERROR"


class EmailAmbiguousRecipientError(EmailIntegrationError):
    code = "INTEGRATION_AMBIGUOUS_TARGET"


class EmailInvalidRecipientError(EmailIntegrationError):
    code = "VALIDATION_FAILED"


class EmailAttachmentError(EmailIntegrationError):
    code = "VALIDATION_FAILED"


class EmailUncertainWriteOutcomeError(EmailIntegrationError):
    code = "INTEGRATION_UNCERTAIN_WRITE_OUTCOME"


class EmailUnsupportedCapabilityError(EmailIntegrationError):
    code = "INTEGRATION_CAPABILITY_UNAVAILABLE"


class EmailNotFoundError(EmailIntegrationError):
    code = "INTEGRATION_NOT_FOUND"
