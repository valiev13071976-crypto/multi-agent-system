import os
from dataclasses import dataclass


RETRYABLE_CODES = frozenset(
    {
        "timeout",
        "execution_timeout",
        "temporary_provider_error",
        "temporary_tool_unavailable",
        "transient_network_error",
        "QueueTimeoutError",
    }
)

NON_RETRYABLE_CODES = frozenset(
    {
        "invalid_mode",
        "invalid_role",
        "finops_budget_denied",
        "no_capable_provider",
        "no_providers_available",
        "provider_not_configured",
        "security_error",
        "permission_denied",
        "malformed_request",
        "workflow_transition_error",
        "InvalidModeError",
        "InvalidRoleError",
        "FinOpsBudgetDeniedError",
        "NoCapableProviderError",
        "NoProvidersAvailableError",
        "ProviderNotConfiguredError",
        "WorkflowTransitionError",
        "QueueCancelledError",
        "execution_outcome_uncertain",
        "financial_execution_not_enabled",
        "pricing_write_not_enabled",
        "customer_communication_execution_not_enabled",
        "permission_change_execution_not_enabled",
        "delete_execution_not_enabled",
        "code_execution_not_enabled",
        "side_effect_execution_denied",
        "side_effect_idempotency_conflict",
        "reconciliation_still_uncertain",
        "reconciliation_manual_review",
    }
)

BACKOFF_FIXED = "fixed"
BACKOFF_EXPONENTIAL = "exponential"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    base_delay_seconds: float = 5.0
    max_delay_seconds: float = 60.0
    backoff_mode: str = BACKOFF_FIXED

    def delay_seconds(self, attempt: int) -> float:
        if self.backoff_mode == BACKOFF_EXPONENTIAL:
            delay = self.base_delay_seconds * (2 ** max(attempt - 1, 0))
        else:
            delay = self.base_delay_seconds
        if delay > self.max_delay_seconds:
            return float(self.max_delay_seconds)
        return float(delay)

    def can_retry(self, attempt: int, error_code: str | None) -> bool:
        if not is_retryable(error_code):
            return False
        return attempt < self.max_attempts

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        mode = (os.getenv("TASK_QUEUE_BACKOFF_MODE") or BACKOFF_FIXED).strip()
        if mode not in {BACKOFF_FIXED, BACKOFF_EXPONENTIAL}:
            mode = BACKOFF_FIXED
        return cls(
            max_attempts=int(os.getenv("TASK_QUEUE_MAX_ATTEMPTS") or "1"),
            base_delay_seconds=float(
                os.getenv("TASK_QUEUE_BASE_RETRY_DELAY_SECONDS") or "5"
            ),
            max_delay_seconds=float(
                os.getenv("TASK_QUEUE_MAX_RETRY_DELAY_SECONDS") or "60"
            ),
            backoff_mode=mode,
        )


def is_retryable(error_code: str | None) -> bool:
    if not error_code:
        return False
    if error_code in NON_RETRYABLE_CODES:
        return False
    return error_code in RETRYABLE_CODES
