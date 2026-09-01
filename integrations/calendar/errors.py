"""Calendar integration errors."""

from __future__ import annotations

from integrations.activation.errors import ActivationError


class CalendarIntegrationError(ActivationError):
    code = "CALENDAR_INTEGRATION_ERROR"


class CalendarAmbiguousTargetError(CalendarIntegrationError):
    code = "INTEGRATION_AMBIGUOUS_TARGET"


class CalendarTimezoneError(CalendarIntegrationError):
    code = "VALIDATION_FAILED"


class CalendarUncertainWriteOutcomeError(CalendarIntegrationError):
    code = "INTEGRATION_UNCERTAIN_WRITE_OUTCOME"


class CalendarUnsupportedCapabilityError(CalendarIntegrationError):
    code = "INTEGRATION_CAPABILITY_UNAVAILABLE"


class CalendarNotFoundError(CalendarIntegrationError):
    code = "INTEGRATION_NOT_FOUND"
