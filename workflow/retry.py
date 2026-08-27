"""Retry classification for workflow steps — reuses queue taxonomy."""

from __future__ import annotations

from task_queue.retry import NON_RETRYABLE_CODES, RETRYABLE_CODES, is_retryable
from workflow.definition import StepRetryPolicy

# Workflow-specific non-retryable additions
WORKFLOW_NON_RETRYABLE = frozenset(
    {
        "validation_error",
        "WorkflowDefinitionError",
        "capability_denied",
        "autonomy_denied",
        "AutonomyDeniedError",
        "hitl_denied",
        "approval_rejected",
        "approval_expired",
        "permanent_config_error",
        "invalid_definition",
        "unknown_dependency",
        "cycle_detected",
        "workflow_cancelled",
        "workflow_deadline_exceeded",
    }
)

WORKFLOW_RETRYABLE = frozenset(
    {
        "timeout",
        "execution_timeout",
        "temporary_provider_error",
        "temporary_tool_unavailable",
        "transient_network_error",
        "QueueTimeoutError",
        "step_timeout",
    }
)


def classify_retryable(
    error_code: str | None,
    *,
    policy: StepRetryPolicy | None = None,
    error_class: str | None = None,
) -> bool:
    if not error_code and not error_class:
        return False
    code = error_code or ""
    cls = error_class or ""
    if policy is not None:
        if code in policy.non_retryable_error_classes or cls in policy.non_retryable_error_classes:
            return False
        if code in policy.retryable_error_classes or cls in policy.retryable_error_classes:
            return True
    if code in WORKFLOW_NON_RETRYABLE or cls in WORKFLOW_NON_RETRYABLE:
        return False
    if code in NON_RETRYABLE_CODES or cls in NON_RETRYABLE_CODES:
        return False
    if code in WORKFLOW_RETRYABLE or cls in WORKFLOW_RETRYABLE:
        return True
    return is_retryable(code) or is_retryable(cls)


def can_retry_attempt(
    attempt: int,
    error_code: str | None,
    policy: StepRetryPolicy,
    *,
    error_class: str | None = None,
) -> bool:
    if not classify_retryable(error_code, policy=policy, error_class=error_class):
        return False
    return int(attempt) < int(policy.max_attempts)
